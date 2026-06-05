#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const Module = require('module');

const MAX_BODY_SIZE = 10 * 1024 * 1024;
const LOCATOR_METHODS = {
  getByLabel: 'label',
  getByPlaceholder: 'placeholder',
  getByText: 'text',
  getByTestId: 'testid',
  getByAltText: 'alttext',
  getByTitle: 'title',
  locator: 'css',
};
const LOCATOR_ACTIONS = {
  fill: 'fill',
  clear: 'clear',
  click: 'click',
  dblclick: 'dblclick',
  hover: 'hover',
  selectOption: 'select',
  check: 'check',
  uncheck: 'uncheck',
  press: 'press',
  setInputFiles: 'upload',
};
const EXPECT_LOCATOR_ACTIONS = {
  toBeVisible: 'assert_visible',
  toBeHidden: 'assert_hidden',
  toHaveText: 'assert_text',
  toHaveValue: 'assert_value',
  toBeEnabled: 'assert_enabled',
  toBeDisabled: 'assert_disabled',
  toHaveCount: 'assert_count',
};

class SkipTestError extends Error {}

function parseInput() {
  const inputPath = process.argv[2];
  if (!inputPath) {
    throw new Error('usage: node run_generated_code.cjs <input.json>');
  }
  return JSON.parse(fs.readFileSync(inputPath, 'utf8'));
}

function resolveMaybeRelativeUrl(targetUrl, value) {
  if (!value || typeof value !== 'string') return value;
  if (value.startsWith('http://') || value.startsWith('https://')) return value;
  if (value.startsWith('/') && targetUrl) return `${targetUrl.replace(/\/$/, '')}${value}`;
  return value;
}

function formatError(action, error) {
  const message = error && error.message ? String(error.message) : String(error || 'unknown');
  if (action === 'navigate') return `TOOL_UI_NAVIGATION_FAIL: ${message}`;
  if (String(action).startsWith('assert')) return `TOOL_UI_ASSERTION_FAIL: ${message}`;
  if (error && (error.name === 'TimeoutError' || /Timeout/i.test(message))) {
    return `TOOL_UI_LOCATOR_NOT_FOUND: ${message}`;
  }
  return `TOOL_UI_UNKNOWN: ${message}`;
}

class JsRunner {
  constructor(config, playwright) {
    this.config = config;
    this.playwright = playwright;
    this.tests = [];
    this.stepResults = [];
    this.apiCalls = [];
    this.consoleLogs = [];
    this.requestTimes = new Map();
    this.currentStepNo = null;
    this.stepCounter = 0;
    this.page = null;
    this.pageProxy = null;
    this.context = null;
    this.browser = null;
    this.expectApi = null;
  }

  registerTest(name, fn) {
    this.tests.push({ name, fn });
  }

  createExports() {
    const testFn = (name, fn) => this.registerTest(name, fn);
    testFn.skip = (condition, message) => {
      if (condition) throw new SkipTestError(message || 'test skipped');
    };

    return {
      test: testFn,
      expect: (subject) => this.createExpectWrapper(subject),
    };
  }

  async setup() {
    this.browser = await this.playwright.chromium.launch({ headless: !!this.config.headless });
    this.context = await this.browser.newContext({
      extraHTTPHeaders: { 'X-Trace-Id': this.config.trace_id },
    });
    this.page = await this.context.newPage();
    this.page.on('console', (msg) => {
      this.consoleLogs.push(`[${msg.type()}] ${msg.text()}`);
    });
    this.page.on('pageerror', (err) => {
      this.consoleLogs.push(`[pageerror] ${err && err.message ? err.message : String(err)}`);
    });
    this.page.on('request', (request) => {
      this.requestTimes.set(request, Date.now());
    });
    this.page.on('response', async (response) => {
      const request = response.request();
      const start = this.requestTimes.get(request) || Date.now();
      this.requestTimes.delete(request);
      const latencyMs = Math.max(0, Date.now() - start);

      let responseBody = null;
      let responseSize = 0;
      try {
        const bodyBuffer = await response.body();
        responseSize = bodyBuffer.length;
        if (bodyBuffer.length <= MAX_BODY_SIZE) {
          const contentType = response.headers()['content-type'] || '';
          if (contentType.includes('json')) {
            responseBody = JSON.parse(bodyBuffer.toString('utf8'));
          }
        }
      } catch (_) {}

      let requestBody = null;
      try {
        const postData = request.postData();
        if (postData) {
          requestBody = JSON.parse(postData);
        }
      } catch (_) {}

      this.apiCalls.push({
        timestamp: new Date().toISOString(),
        method: request.method(),
        url: response.url(),
        request_headers: request.headers(),
        request_body: requestBody,
        status_code: response.status(),
        response_body: responseBody,
        response_size: responseSize,
        content_type: response.headers()['content-type'] || '',
        latency_ms: latencyMs,
        matched_step_no: this.currentStepNo,
      });
    });

    this.pageProxy = this.createPageProxy(this.page);
  }

  async teardown() {
    if (this.context) await this.context.close();
    if (this.browser) await this.browser.close();
  }

  createPageProxy(page) {
    return new Proxy(page, {
      get: (target, prop) => {
        if (prop === '__qapilot_page__') return true;
        if (prop in LOCATOR_METHODS) {
          return (...args) => {
            const locator = target[prop](...args);
            const selector = args[0] == null ? '' : String(args[0]);
            return this.wrapLocator(locator, { selector_type: LOCATOR_METHODS[prop], selector });
          };
        }
        if (prop === 'goto') {
          return async (url, ...rest) => {
            const resolved = resolveMaybeRelativeUrl(this.config.target_url, url);
            return this.runStep({
              action: 'navigate',
              selector: null,
              selector_type: null,
              value: resolved,
              expected: null,
            }, async () => target.goto(resolved, ...rest));
          };
        }
        if (prop === 'reload') {
          return async (...args) => this.runStep({ action: 'reload', selector: null, selector_type: null, value: null, expected: null }, async () => target.reload(...args));
        }
        if (prop === 'goBack') {
          return async (...args) => this.runStep({ action: 'go_back', selector: null, selector_type: null, value: null, expected: null }, async () => target.goBack(...args));
        }
        if (prop === 'goForward') {
          return async (...args) => this.runStep({ action: 'go_forward', selector: null, selector_type: null, value: null, expected: null }, async () => target.goForward(...args));
        }
        if (prop === 'waitForTimeout') {
          return async (value) => this.runStep({ action: 'wait', selector: null, selector_type: null, value, expected: null }, async () => target.waitForTimeout(value));
        }
        if (prop === 'waitForURL') {
          return async (value, ...args) => this.runStep({ action: 'wait_for_url', selector: null, selector_type: null, value, expected: null }, async () => target.waitForURL(value, ...args));
        }
        if (prop === 'waitForLoadState') {
          return async (value, ...args) => this.runStep({ action: 'wait_for_load_state', selector: null, selector_type: null, value, expected: null }, async () => target.waitForLoadState(value, ...args));
        }
        const value = target[prop];
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
  }

  wrapLocator(locator, meta) {
    const proxy = new Proxy(locator, {
      get: (target, prop) => {
        if (prop === '__qapilot_locator__') return { locator: target, meta };
        if (prop === 'locator') {
          return (...args) => this.wrapLocator(target.locator(...args), { selector_type: 'css', selector: String(args[0] || '') });
        }
        if (prop === 'nth') {
          return (...args) => this.wrapLocator(target.nth(...args), meta);
        }
        if (prop in LOCATOR_ACTIONS) {
          return async (...args) => {
            const action = LOCATOR_ACTIONS[prop];
            const value = ['fill', 'select', 'press', 'upload'].includes(action) ? args[0] : null;
            return this.runStep({
              action,
              selector: meta.selector,
              selector_type: meta.selector_type,
              value,
              expected: null,
            }, async () => target[prop](...args));
          };
        }
        const value = target[prop];
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    return proxy;
  }

  createExpectWrapper(subject) {
    if (subject && subject.__qapilot_page__) {
      return {
        toHaveURL: async (expected, ...args) => this.runStep({
          action: 'assert_url',
          selector: null,
          selector_type: null,
          value: null,
          expected,
        }, async () => this.playwright.expect(this.page).toHaveURL(expected, ...args)),
      };
    }
    if (subject && subject.__qapilot_locator__) {
      const { locator, meta } = subject.__qapilot_locator__;
      const expectTarget = this.playwright.expect(locator);
      const api = {};
      for (const [method, action] of Object.entries(EXPECT_LOCATOR_ACTIONS)) {
        api[method] = async (expected, ...args) => this.runStep({
          action,
          selector: meta.selector,
          selector_type: meta.selector_type,
          value: null,
          expected: ['assert_text', 'assert_value', 'assert_count'].includes(action) ? expected : null,
        }, async () => expectTarget[method](expected, ...args));
      }
      return api;
    }
    return this.playwright.expect(subject);
  }

  async runStep(step, executor) {
    const stepNo = ++this.stepCounter;
    const start = Date.now();
    this.currentStepNo = stepNo;
    let status = 'pass';
    let error = null;
    process.stderr.write(`[step:start] tc=${this.config.tc_id} step=${stepNo} action=${step.action}\n`);
    try {
      await executor();
    } catch (err) {
      if (err instanceof SkipTestError) {
        status = 'skip';
        error = err.message;
      } else {
        status = 'fail';
        error = formatError(step.action, err);
      }
    }

    let screenshotPath = null;
    try {
      fs.mkdirSync(this.config.screenshots_dir, { recursive: true });
      screenshotPath = path.join(this.config.screenshots_dir, `step_${String(stepNo).padStart(2, '0')}.png`);
      await this.page.screenshot({ path: screenshotPath });
    } catch (_) {
      screenshotPath = null;
    }

    const duration = Date.now() - start;
    process.stderr.write(`[step:end]   tc=${this.config.tc_id} step=${stepNo} action=${step.action} status=${status} duration=${duration}ms${error ? ` error=${error}` : ''}\n`);
    this.stepResults.push({
      step_no: stepNo,
      action: step.action,
      status,
      screenshot_path: screenshotPath,
      console_logs: [...this.consoleLogs],
      error,
      duration_ms: duration,
    });
    this.currentStepNo = null;

    if (status !== 'pass') {
      if (status === 'skip') throw new SkipTestError(error || 'test skipped');
      throw new Error(error || 'step failed');
    }
  }

  async executeRegisteredTests() {
    const started = Date.now();
    let skipReason = null;
    let runtimeError = null;
    process.stderr.write(`[tc:start] tc=${this.config.tc_id}\n`);
    try {
      if (!this.tests.length) {
        throw new Error('generated code 에 등록된 test(...) 가 없습니다.');
      }
      const first = this.tests[0];
      await first.fn({ page: this.pageProxy });
    } catch (err) {
      if (err instanceof SkipTestError) {
        skipReason = err.message;
      } else {
        runtimeError = err;
      }
    }

    if (!this.stepResults.length && skipReason) {
      this.stepResults.push({
        step_no: 1,
        action: 'skip',
        status: 'skip',
        screenshot_path: null,
        console_logs: [...this.consoleLogs],
        error: skipReason,
        duration_ms: 0,
      });
    }

    const uiStatus = this.stepResults.every((step) => step.status === 'pass') ? 'pass' : 'fail';
    process.stderr.write(`[tc:end]   tc=${this.config.tc_id} status=${uiStatus} duration=${Date.now() - started}ms\n`);
    const payload = {
      ui_result: {
        tc_id: this.config.tc_id,
        status: uiStatus,
        steps: this.stepResults,
        total_duration_ms: Date.now() - started,
      },
      api_result: {
        tc_id: this.config.tc_id,
        calls: this.apiCalls,
        total_calls: this.apiCalls.length,
        error_calls: this.apiCalls.filter((call) => Number(call.status_code || 0) >= 400).length,
      },
    };

    if (runtimeError && !this.stepResults.length) {
      throw runtimeError;
    }
    return payload;
  }
}

async function main() {
  const config = parseInput();
  let playwright;
  let pwExpect;
  try {
    playwright = require('playwright');
    ({ expect: pwExpect } = require('playwright/test'));
  } catch (err) {
    throw new Error(
      "playwright / playwright/test npm 패키지를 찾을 수 없습니다. `qapilot/qapilot/assets/js_runner` 에서 `npm install` 이 필요합니다."
    );
  }

  playwright.expect = pwExpect;
  const runner = new JsRunner(config, playwright);
  await runner.setup();

  const exportsObj = runner.createExports();
  const originalLoad = Module._load;
  try {
    Module._load = function patched(request, parent, isMain) {
      if (request === '@playwright/test') {
        return exportsObj;
      }
      return originalLoad.apply(this, arguments);
    };
    require(path.resolve(config.code_file));
    const output = await runner.executeRegisteredTests();
    fs.writeFileSync(config.output_file, JSON.stringify(output, null, 2), 'utf8');
  } finally {
    Module._load = originalLoad;
    await runner.teardown();
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
