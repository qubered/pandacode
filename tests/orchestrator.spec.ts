import { expect, test, type Page } from '@playwright/test';

// Hermes orchestrator UI in the browser preview: the sample "Explore the project" task
// (session-4) is supervised by Hermes and carries one message Hermes sent.
const calls = (page: Page, command: string): Promise<any[]> => page.evaluate(command => ((window as any).__orchCalls ?? []).filter((entry: any) => entry.command === command), command);
const row = (page: Page, id: string) => page.locator(`.task-item[data-id="${id}"]`);
const orch = (page: Page) => page.locator('.orchestrator-settings');
const settings = (page: Page) => page.getByRole('region', { name: 'Settings', exact: true });

async function open(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as any).__clipboard = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { (window as any).__clipboard.push(text); } } });
  });
  await page.route('**/src/preview.ts*', async route => {
    const response = await route.fetch();
    const source = await response.text();
    const marker = /switch\s*\(command\)\s*\{/;
    expect(source).toMatch(marker);
    await route.fulfill({ response, body: source.replace(marker, `
      window.__orchCalls ??= [];
      window.__orchEmit ??= event => agent(event);
      if (command.startsWith('orchestrator_') || command === 'set_task_supervisor') window.__orchCalls.push({ command, args: structuredClone(args ?? {}) });
      switch (command) {
    `) });
  });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.task-item.selected')).toHaveCount(1);
}

async function openSettings(page: Page): Promise<void> {
  await page.keyboard.press('Meta+,');
  await settings(page).getByRole('tab', { name: 'Agents', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: 'Enable orchestrator' })).toBeVisible();
}

test.describe('Hermes supervision', () => {
  test('a supervised task carries a badge, and others do not', async ({ page }) => {
    await open(page);
    const badge = row(page, 'session-4').locator('.task-supervisor');
    await expect(badge).toHaveText('Hermes');
    await expect(badge).toHaveAttribute('title', 'Supervised by Hermes');
    await expect(row(page, 'session-1').locator('.task-supervisor')).toHaveCount(0);
    // Rows keep their shape: the badge adds no line.
    const heights = await Promise.all(['session-1', 'session-4'].map(id => row(page, id).evaluate(node => node.getBoundingClientRect().height)));
    expect(Math.abs(heights[0] - heights[1])).toBeLessThan(2);
  });

  test('the header names the supervisor, and Take over and Let Hermes supervise hand the task back and forth', async ({ page }) => {
    await open(page);
    await expect(page.locator('.supervisor-crumb')).toHaveCount(0);
    await row(page, 'session-4').click();
    await expect(page.locator('.supervisor-crumb .supervisor-label')).toHaveText('Supervised by Hermes');
    await page.getByRole('button', { name: 'Take over', exact: true }).click();
    await expect.poll(async () => (await calls(page, 'set_task_supervisor')).map(call => call.args)).toEqual([{ sessionId: 'session-4', supervisor: null }]);
    await expect(row(page, 'session-4').locator('.task-supervisor')).toHaveCount(0);
    await expect(page.locator('.supervisor-crumb')).toHaveCount(0);

    await page.getByRole('button', { name: 'Task actions', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Let Hermes supervise', exact: true }).click();
    await expect.poll(async () => (await calls(page, 'set_task_supervisor')).map(call => call.args).at(-1)).toEqual({ sessionId: 'session-4', supervisor: 'hermes' });
    await expect(row(page, 'session-4').locator('.task-supervisor')).toHaveText('Hermes');
    await expect(page.locator('.supervisor-crumb .supervisor-label')).toHaveText('Supervised by Hermes');
  });

  test('an unsupervised task offers Let Hermes supervise only while the orchestrator is on', async ({ page }) => {
    await open(page);
    await row(page, 'session-1').click();
    await page.getByRole('button', { name: 'Task actions', exact: true }).click();
    await expect(page.getByRole('menuitem', { name: 'Let Hermes supervise', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');

    await openSettings(page);
    await page.getByRole('checkbox', { name: 'Enable orchestrator' }).uncheck();
    await expect.poll(async () => (await calls(page, 'orchestrator_save_settings')).at(-1)?.args.settings.enabled).toBe(false);
    await settings(page).getByRole('button', { name: 'Back to the workspace', exact: true }).click();
    await page.getByRole('button', { name: 'Task actions', exact: true }).click();
    await expect(page.getByRole('menuitem', { name: 'Let Hermes supervise', exact: true })).toHaveCount(0);
  });

  test('messages Hermes sent show its name instead of You', async ({ page }) => {
    await open(page);
    await row(page, 'session-4').click();
    const message = page.locator('#transcript .user-message');
    await expect(message).toHaveCount(1);
    await expect(message.locator('.message-meta strong')).toHaveText('Hermes');
    await expect(message).toContainText('Summarize what this project does.');
    await row(page, 'session-1').click();
    await expect(page.locator('#transcript .user-message .message-meta strong').first()).toHaveText('You');
  });

  test('approvals and questions on a supervised task say Hermes is supervising', async ({ page }) => {
    await open(page);
    await row(page, 'session-4').click();
    await page.evaluate(() => (window as any).__orchEmit({ sessionId: 'session-4', kind: 'approval', requestId: 'run:approve-1', text: 'Run `npm test`?' }));
    await expect(page.locator('.approval-bar .supervisor-note')).toHaveText('Hermes is supervising — you can still answer');
    await expect(page.getByRole('button', { name: 'Approve', exact: true })).toBeEnabled();
    await page.evaluate(() => (window as any).__orchEmit({ sessionId: 'session-4', kind: 'approval_cancelled', requestId: 'run:approve-1' }));
    await expect(page.locator('.approval-bar')).toHaveCount(0);

    await page.evaluate(() => (window as any).__orchEmit({ sessionId: 'session-4', kind: 'input_request', requestId: 'run:ask-1', data: { kind: 'questions', questions: [{ id: 'theme', header: 'Theme', question: 'Which theme?', options: [{ label: 'Yellow', description: 'Warm' }] }] } }));
    await expect(page.locator('.agent-input .supervisor-note')).toHaveText('Hermes is supervising — you can still answer');

    await row(page, 'session-1').click();
    await page.evaluate(() => (window as any).__orchEmit({ sessionId: 'session-1', kind: 'approval', requestId: 'run:approve-2', text: 'Run `ls`?' }));
    await expect(page.locator('.approval-bar')).toBeVisible();
    await expect(page.locator('.supervisor-note')).toHaveCount(0);
  });
});

test.describe('Settings → Agents → Orchestrator', () => {
  test('saves what was filled in, reloads it, and keeps the webhook secret out of the page', async ({ page }) => {
    await open(page);
    await openSettings(page);
    await expect(page.locator('.orchestrator-state')).toContainText('Running');
    await expect(orch(page).getByLabel('Orchestrator name', { exact: true })).toHaveValue('hermes');
    await expect(orch(page).getByLabel('Port', { exact: true })).toHaveValue('7879');
    await expect(page.locator('.orchestrator-url')).toHaveText('http://127.0.0.1:7879/mcp');
    await expect(orch(page).getByLabel('Highest mode', { exact: true })).toHaveValue('auto');

    await orch(page).getByLabel('Address', { exact: true }).fill('100.87.96.22');
    await orch(page).getByLabel('Port', { exact: true }).fill('7900');
    await orch(page).getByLabel('Highest mode', { exact: true }).selectOption('ask');
    await orch(page).getByLabel('Webhook URL', { exact: true }).fill('http://hermes-host:8644/webhooks/pandacode');
    await orch(page).getByLabel('Webhook secret', { exact: true }).fill('s3cret-value');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('.orchestrator-saved')).toHaveText('Saved');
    const save = (await calls(page, 'orchestrator_save_settings')).at(-1);
    expect(save.args).toEqual({
      settings: { enabled: true, name: 'hermes', bind: '100.87.96.22', port: 7900, maxMode: 'ask', webhook: { url: 'http://hermes-host:8644/webhooks/pandacode' } },
      webhookSecret: 's3cret-value',
    });
    await expect(page.locator('.orchestrator-url')).toHaveText('http://100.87.96.22:7900/mcp');
    await expect(orch(page).getByLabel('Webhook secret', { exact: true })).toHaveValue('');
    await expect(orch(page).getByLabel('Webhook secret', { exact: true })).toHaveAttribute('placeholder', /Saved/);
    expect(await page.locator('body').innerHTML()).not.toContain('s3cret-value');

    // Saving again without typing a secret keeps the saved one (no webhookSecret argument).
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect.poll(async () => (await calls(page, 'orchestrator_save_settings')).length).toBe(2);
    expect((await calls(page, 'orchestrator_save_settings')).at(-1).args).not.toHaveProperty('webhookSecret');

    // Closing and reopening Settings reads the saved state back.
    await settings(page).getByRole('button', { name: 'Back to the workspace', exact: true }).click();
    await openSettings(page);
    await expect(orch(page).getByLabel('Address', { exact: true })).toHaveValue('100.87.96.22');
    await expect(orch(page).getByLabel('Port', { exact: true })).toHaveValue('7900');
    await expect(orch(page).getByLabel('Highest mode', { exact: true })).toHaveValue('ask');
    await expect(orch(page).getByLabel('Webhook URL', { exact: true })).toHaveValue('http://hermes-host:8644/webhooks/pandacode');

    // Remove sends an empty secret.
    await page.getByRole('button', { name: 'Remove', exact: true }).click();
    await expect.poll(async () => (await calls(page, 'orchestrator_save_settings')).at(-1).args.webhookSecret).toBe('');
    await expect(page.getByRole('button', { name: 'Remove', exact: true })).toBeHidden();
  });

  test('shows save errors inline', async ({ page }) => {
    await open(page);
    await openSettings(page);
    await orch(page).getByLabel('Orchestrator name', { exact: true }).fill('not valid!');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('.orchestrator-error')).toHaveText('Use a short name of letters, numbers, - or _.');
    await orch(page).getByLabel('Orchestrator name', { exact: true }).fill('hermes');
    await orch(page).getByLabel('Address', { exact: true }).fill('not-an-ip');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('.orchestrator-error')).toHaveText('The address must be an IP address.');
  });

  test('Copy token reveals the token to the clipboard only, and the config snippet is copyable', async ({ page }) => {
    await open(page);
    await openSettings(page);
    await page.getByRole('button', { name: 'Copy token', exact: true }).click();
    await expect(page.locator('.orchestrator-saved')).toHaveText('Token copied');
    expect(await calls(page, 'orchestrator_reveal_token')).toHaveLength(1);
    expect(await page.evaluate(() => (window as any).__clipboard)).toEqual(['preview-orchestrator-token']);
    expect(await page.locator('body').innerHTML()).not.toContain('preview-orchestrator-token');

    const snippet = page.locator('.orchestrator-snippet');
    await expect(snippet).toContainText('mcp_servers:');
    await expect(snippet).toContainText('url: "http://127.0.0.1:7879/mcp"');
    await expect(snippet).toContainText('Authorization: "Bearer <token>"');
    await page.getByRole('button', { name: 'Copy snippet', exact: true }).click();
    await expect(page.locator('.orchestrator-saved')).toHaveText('Snippet copied');
    expect(await page.evaluate(() => (window as any).__clipboard.at(-1))).toContain('Bearer <token>');
  });
});
