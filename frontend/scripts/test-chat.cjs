// Free regression checks of the real reducer, localization and rendered cards.
const assert = require('node:assert/strict');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { execFileSync } = require('node:child_process');
const out = mkdtempSync(join(tmpdir(), 'nabd-chat-'));
try {
  execFileSync(process.execPath, ['node_modules/typescript/bin/tsc', '--target', 'es2020', '--module', 'commonjs',
    '--jsx', 'react-jsx', '--esModuleInterop', '--skipLibCheck', '--outDir', out,
    'src/chat/reducer.ts', 'src/chat/messages.ts', 'src/chat/ActivityPanel.tsx'], { stdio: 'inherit' });
  // Compiled React module resolves dependencies from this project's node_modules.
  require('node:module').Module._initPaths();
  const { emptyState, applyEvents, fromSnapshot } = require(join(out, 'reducer.js'));
  const { t, direction, localeOf, ref } = require(join(out, 'i18n.js'));
  const { activityTitle, activityDetail, runHeadline, caseLine } = require(join(out, 'messages.js'));
  const language = { input_language: 'ary', reply_language: 'ary', reply_script: 'arabic' };
  const event = (sequence, type, payload, run = 'r1') => ({ sequence, id: String(sequence), type, payload,
    run_id: run, conversation_id: 'c', created_at: '2026-11-18T12:00:00Z' });
  let state = applyEvents(emptyState(), [event(1, 'run.queued', { message_id: 'm' }), event(2, 'conversation.language', language)]);
  assert.equal(localeOf(state.language), 'ary-arab');
  assert.equal(direction(localeOf(state.runs.r1.language)), 'rtl');
  assert.deepEqual(applyEvents(state, [event(2, 'conversation.language', { reply_language: 'fr', reply_script: 'latin' })]), state);
  state = applyEvents(state, [event(3, 'conversation.language', { input_language: 'fr', reply_language: 'fr', reply_script: 'latin' }, 'r2')]);
  assert.equal(localeOf(state.language), 'fr');
  assert.equal(localeOf(state.runs.r1.language), 'ary-arab');
  assert.equal(localeOf(state.runs.r2.language), 'fr');
  const restored = fromSnapshot({ conversation: {}, last_sequence: 3, messages: [], runs: [], partial: {}, cases: [],
    events: [event(2, 'conversation.language', language)], language: state.language });
  assert.equal(localeOf(restored.language), 'fr'); // latest language survives a bounded activity snapshot
  assert.equal(localeOf(restored.runs.r1.language), 'ary-arab');
  const codes = ['agent.supervisor', 'agent.orders', 'agent.complaints', 'tool.list_orders', 'tool.get_order',
    'tool.list_knowledge', 'tool.get_knowledge', 'tool.list_cases', 'tool.list_case_history', 'tool.get_customer_profile',
    'case.create', 'case.update', 'case.reopen', 'case.request_closure', 'case.close'];
  for (const locale of ['en', 'fr', 'ar', 'ary-latn', 'ary-arab']) {
    for (const code of codes) for (const status of ['running', 'waiting', 'retrying', 'completed', 'failed']) {
      const activity = { code, state: status, params: { order_number: 'SE-1061', case_number: '00002001' },
        result: { case_number: '00002001', found: true, count: 2, outcome: 'issues', issues: 2 }, error: 'version_changed' };
      assert.ok(activityTitle(activity, locale));
      assert.ok(!activityTitle(activity, locale).includes('undefined'));
      if (status === 'retrying' || status === 'failed') assert.ok(activityDetail(activity, locale));
    }
    for (const status of ['queued', 'running', 'retrying', 'completed', 'failed']) assert.ok(runHeadline({ status, attempt: 2 }, locale));
    assert.ok(caseLine({ case_number: '00002001', team: 'Delivery', case_status: 'Working' }, locale).includes('00002001'));
    assert.ok(!t(locale, 'greeting', { name: ref('Sara') }).includes('{'));
  }
  // JSX render checks language and RTL on the actual card component.
  const Module = require('node:module');
  const originalPaths = Module._nodeModulePaths;
  Module._nodeModulePaths = function(p) { return [...originalPaths(p), join(process.cwd(), 'node_modules')]; };
  const React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const Panel = require(join(out, 'ActivityPanel.js')).default;
  const html = renderToStaticMarkup(React.createElement(Panel, { run: { id: 'r', status: 'queued', attempt: 1, language },
    activities: [], now: 0, expanded: true, onToggle() {} }));
  assert.ok(html.includes('dir="rtl"') && html.includes('lang="ary"') && html.includes('النوبة'));
  console.log('Chat checks passed: language switching, replay, snapshot, templates and RTL rendering.');
} finally { rmSync(out, { recursive: true, force: true }); }
