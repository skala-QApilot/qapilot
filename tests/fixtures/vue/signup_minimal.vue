<template>
  <div class="signup-page">
    <form @submit.prevent="onSubmit">
      <input
        type="text"
        v-model="form.name"
        required
        placeholder="이름"
        data-testid="name"
      />
      <input
        type="email"
        v-model="form.email"
        required
        data-testid="email"
      />
      <div v-if="isMinor" class="guardian-wrap" data-testid="guardian-wrap">
        <input
          type="checkbox"
          v-model="form.guardian_consent"
          data-testid="guardian-consent"
        />
      </div>
      <div v-if="error" class="error" data-testid="error-msg">{{ error }}</div>
      <button
        type="submit"
        :disabled="loading || (isMinor && !form.guardian_consent)"
        data-testid="submit"
      >
        가입
      </button>
    </form>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue';
const form = ref({ name: '', email: '', guardian_consent: false });
const loading = ref(false);
const error = ref('');
const isMinor = computed(() => false);
const onSubmit = () => {};
</script>
