"""Server-rendered fallback project dashboard page."""

from __future__ import annotations

from fastapi.responses import HTMLResponse


def build_project_dashboard_html(project_slug: str) -> HTMLResponse:
    """Return a small HTML shell that loads the project details via the API."""
    html = f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>QApilot - {project_slug}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #0b1020;
        --panel: rgba(255, 255, 255, 0.08);
        --panel-border: rgba(255, 255, 255, 0.14);
        --text: #f4f7fb;
        --muted: #aab3c5;
        --accent: #7c5cff;
        --accent-soft: rgba(124, 92, 255, 0.18);
      }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; min-height: 100vh; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top, #1b2350, var(--bg) 55%); color: var(--text); }}
      main {{ max-width: 1100px; margin: 0 auto; padding: 48px 20px 64px; }}
      .hero {{ display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 24px; }}
      .eyebrow {{ text-transform: uppercase; letter-spacing: .18em; color: var(--muted); font-size: 12px; margin-bottom: 10px; }}
      h1 {{ margin: 0; font-size: clamp(32px, 5vw, 54px); line-height: 1.02; }}
      .subtitle {{ color: var(--muted); margin-top: 12px; max-width: 720px; line-height: 1.6; }}
      .pill {{ display: inline-flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: 999px; background: var(--accent-soft); color: #dcd6ff; border: 1px solid rgba(124, 92, 255, 0.35); font-size: 14px; }}
      .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; margin-top: 24px; }}
      .card {{ grid-column: span 6; background: var(--panel); border: 1px solid var(--panel-border); border-radius: 24px; padding: 20px; backdrop-filter: blur(14px); }}
      .card h2 {{ margin: 0 0 14px; font-size: 18px; }}
      .meta {{ display: grid; gap: 12px; color: var(--muted); font-size: 14px; }}
      .meta strong {{ color: var(--text); font-weight: 600; }}
      .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
      .kpi {{ border-radius: 18px; padding: 16px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); }}
      .kpi .label {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
      .kpi .value {{ font-size: 28px; font-weight: 700; }}
      .endpoints {{ display: grid; gap: 10px; }}
      .endpoint {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 12px 14px; border-radius: 16px; background: rgba(255,255,255,0.05); }}
      .endpoint code {{ color: #fff; font-weight: 600; }}
      .empty {{ color: var(--muted); padding: 12px 0; }}
      .status {{ display: inline-flex; align-items: center; gap: 8px; color: #d4ffd9; background: rgba(74, 222, 128, 0.14); border: 1px solid rgba(74, 222, 128, 0.32); padding: 8px 12px; border-radius: 999px; font-size: 13px; }}
      .status.off {{ color: #ffd7d7; background: rgba(248, 113, 113, 0.16); border-color: rgba(248, 113, 113, 0.3); }}
      @media (max-width: 900px) {{ .card {{ grid-column: span 12; }} .hero {{ flex-direction: column; align-items: start; }} .kpis {{ grid-template-columns: 1fr; }} }}
    </style>
  </head>
  <body>
    <main>
      <div class="hero">
        <div>
          <div class="eyebrow">QApilot project dashboard</div>
          <h1 id="project-name">{project_slug}</h1>
          <p class="subtitle" id="project-status">프로젝트 정보를 불러오는 중입니다.</p>
        </div>
        <div class="pill" id="project-pill">/{project_slug}</div>
      </div>

      <div class="grid">
        <section class="card">
          <h2>프로젝트 메타데이터</h2>
          <div class="meta" id="project-meta"></div>
        </section>
        <section class="card">
          <h2>인덱스 요약</h2>
          <div class="kpis">
            <div class="kpi"><div class="label">파일 수</div><div class="value" id="file-count">-</div></div>
            <div class="kpi"><div class="label">엔드포인트 수</div><div class="value" id="endpoint-count">-</div></div>
            <div class="kpi"><div class="label">모델 수</div><div class="value" id="model-count">-</div></div>
          </div>
          <p class="subtitle" id="scan-time" style="margin-top: 14px;">최근 스캔 시각: -</p>
        </section>
        <section class="card" style="grid-column: span 12;">
          <h2>주요 엔드포인트</h2>
          <div class="endpoints" id="endpoint-list"></div>
        </section>
      </div>
    </main>
    <script>
      const slug = {project_slug!r};
      const metaTarget = document.getElementById('project-meta');
      const endpointList = document.getElementById('endpoint-list');
      const statusTarget = document.getElementById('project-status');
      const pillTarget = document.getElementById('project-pill');
      const fileCount = document.getElementById('file-count');
      const endpointCount = document.getElementById('endpoint-count');
      const modelCount = document.getElementById('model-count');
      const scanTime = document.getElementById('scan-time');
      const nameTarget = document.getElementById('project-name');

      function renderNotRegistered() {{
        statusTarget.textContent = '등록되지 않은 프로젝트입니다.';
        statusTarget.className = 'subtitle';
        metaTarget.innerHTML = '<div class="empty">프로젝트가 등록되지 않았습니다.</div>';
        endpointList.innerHTML = '<div class="empty">표시할 엔드포인트가 없습니다.</div>';
        fileCount.textContent = '-';
        endpointCount.textContent = '-';
        modelCount.textContent = '-';
        scanTime.textContent = '최근 스캔 시각: -';
        pillTarget.textContent = '/' + slug;
      }}

      fetch('/api/projects/' + encodeURIComponent(slug))
        .then(async (response) => {{
          if (!response.ok) {{
            throw new Error('not-found');
          }}
          return response.json();
        }})
        .then((data) => {{
          const project = data.project || {{}};
          const summary = data.summary || {{}};
          nameTarget.textContent = project.display_name || slug;
          statusTarget.textContent = (project.framework || 'unknown') + ' · ' + (project.language || 'unknown');
          statusTarget.className = 'status';
          pillTarget.textContent = '/' + slug;
          metaTarget.innerHTML = [
            ['프로젝트 slug', project.project_slug],
            ['표시 이름', project.display_name],
            ['로컬 경로', project.local_path],
            ['설정 경로', project.config_path],
            ['인덱스 경로', project.index_path],
            ['framework / language', (project.framework || '-') + ' / ' + (project.language || '-')],
            ['등록 시각', project.created_at],
            ['갱신 시각', project.updated_at],
          ].map(([label, value]) => '<div><strong>' + label + '</strong><br/>' + (value || '-') + '</div>').join('');
          fileCount.textContent = String(summary.file_count ?? 0);
          endpointCount.textContent = String(summary.endpoint_count ?? 0);
          modelCount.textContent = String(summary.model_count ?? 0);
          scanTime.textContent = '최근 스캔 시각: ' + (summary.last_scanned_at || project.updated_at || '-');
          const endpoints = Array.isArray(summary.endpoints) ? summary.endpoints : [];
          endpointList.innerHTML = endpoints.length
            ? endpoints.map((item) => '<div class="endpoint"><div><code>' + (item.method || 'ANY') + '</code> ' + (item.path || '-') + '</div><div>' + (item.handler || item.file || '') + '</div></div>').join('')
            : '<div class="empty">표시할 엔드포인트가 없습니다.</div>';
        }})
        .catch(() => {{
          renderNotRegistered();
          statusTarget.className = 'status off';
        }});
    </script>
  </body>
</html>"""
    return HTMLResponse(content=html)
