const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  timeout: 45000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: process.env.DEMO_BASE_URL || 'http://127.0.0.1:10000',
    headless: true,
    trace: 'retain-on-failure'
  },
  reporter: [['list']]
});
