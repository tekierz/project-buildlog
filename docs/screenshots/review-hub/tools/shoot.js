// node shoot.js BASE_URL SINCE OUT_DIR [only,names]   the demo screenshots (made-up project data)
const puppeteer = require('puppeteer-core');
const [BASE, SINCE, OUT, ONLY] = process.argv.slice(2);
const HUB = BASE + '/docs/review/index.html';
const only = ONLY ? ONLY.split(',') : null;
const wait = ms => new Promise(r => setTimeout(r, ms));

async function page(b, opts) {
  const p = await b.newPage();
  await p.setViewport({width: opts && opts.w || 1360, height: opts && opts.h || 900, deviceScaleFactor: 1});
  p.on('pageerror', e => console.log('pageerror:', e.message));
  p.on('console', m => { if (m.type() === 'error' && !/favicon|404/.test(m.text())) console.log('console.error:', m.text()); });
  await p.goto(BASE + '/api/version');
  await p.evaluate((s) => { sessionStorage.clear(); localStorage.clear(); localStorage.setItem('review.visit:/docs/review/index.html', s); }, SINCE);
  return p;
}
async function hub(p, hash) {
  await p.goto(HUB + (hash || ''), {waitUntil: 'networkidle0'});
  await wait(700);
}
async function shotEl(p, sel, name, pad) {
  const el = await p.$(sel);
  if (!el) throw new Error('no ' + sel);
  await p.evaluate(() => { const h = document.querySelector('header.top'); if (h) h.style.position = 'static'; });
  await el.evaluate(n => n.scrollIntoView({block: 'start'}));
  await wait(300);
  await el.screenshot({path: OUT + '/' + name + '.png', captureBeyondViewport: true});
  await p.evaluate(() => { const h = document.querySelector('header.top'); if (h) h.style.position = ''; });
  console.log('wrote', name);
}
async function shot(p, name, full) { await p.screenshot({path: OUT + '/' + name + '.png', fullPage: !!full}); console.log('wrote', name); }

const scenes = {
  // ---- version chains, notes carried over, decisions inside their document, new since your last visit
  async 'v01-needs-you-grouped'(b) { const p = await page(b); await hub(p); await shot(p, 'v01-needs-you-grouped'); },
  async 'v02-current-version-carried-notes'(b) {
    const p = await page(b); await hub(p, '#item=script-v3');
    await p.evaluate(() => { document.querySelectorAll('#script-v3 details.earlier').forEach(d => d.open = false); });
    await shotEl(p, '#script-v3', 'v02-current-version-carried-notes');
  },
  async 'v03-earlier-versions'(b) {
    const p = await page(b); await hub(p, '#item=script-v2');
    await wait(400);
    await shotEl(p, '#script-v3 > details.earlier.versions', 'v03-earlier-versions');
  },
  async 'v04-old-version-page'(b) {
    const p = await page(b);
    await p.goto(BASE + '/docs/review/docs/film--harbor_lights_v2.screen.html?item=script-v2', {waitUntil: 'networkidle0'});
    await wait(900);
    await p.evaluate(() => { const m = document.querySelector('.marker'); if (m) m.click(); });
    await wait(300);
    await shot(p, 'v04-old-version-page');
  },
  async 'v05-decisions-in-their-document'(b) {
    const p = await page(b); await hub(p, '#view=answered');
    await shotEl(p, '#script-v3', 'v05-decisions-in-their-document');
  },
  async 'v06-bible-notes-carried'(b) {
    const p = await page(b); await hub(p, '#item=bible-v2');
    await shotEl(p, '#bible-v2', 'v06-bible-notes-carried');
  },
  async 'v07-notes-follow-their-lines'(b) {
    const p = await page(b);
    await p.goto(BASE + '/docs/review/docs/film--harbor_lights_v3.screen.html?item=script-v3', {waitUntil: 'networkidle0'});
    await wait(1000);
    await p.evaluate(() => { document.querySelector('[data-rail="all"]').click(); const r = document.querySelectorAll('.ri')[1]; if (r) r.click(); });
    await wait(900);
    await shot(p, 'v07-notes-follow-their-lines');
  },
  // ---- the hub's other features (the earlier PR), on the same demo project
  async 'f01-kind-filter'(b) {
    const p = await page(b); await hub(p);
    await p.click('[data-facet="kind"] > button'); await wait(250);
    await p.screenshot({path: OUT + '/f01-kind-filter.png', clip: {x: 0, y: 0, width: 1360, height: 520}}); console.log('wrote f01');
  },
  async 'f02-snooze-menu'(b) {
    const p = await page(b); await hub(p, '#item=animatic-sc2');
    await p.click('#animatic-sc2 .later'); await wait(250);
    await shot(p, 'f02-snooze-menu');
  },
  async 'f03-snoozed-view'(b) { const p = await page(b); await hub(p, '#view=snoozed'); await shot(p, 'f03-snoozed-view'); },
  async 'f04-video-and-audio-players'(b) {
    const p = await page(b); await hub(p, '#item=animatic-sc2&media=media/animatic_sc2.mp4&t=2.4');
    await wait(900);
    await p.evaluate(() => { const w = document.querySelector('#animatic-sc2 .ap .wave'); if (w && w.decode) w.decode(); });
    await wait(1200);
    await shotEl(p, '#animatic-sc2', 'f04-video-and-audio-players');
  },
  async 'f05-audition-a-b'(b) {
    const p = await page(b); await hub(p, '#item=wick-voice');
    await p.evaluate(() => document.querySelectorAll('#wick-voice .wave').forEach(w => w.decode && w.decode()));
    await wait(1500);
    await shotEl(p, '#wick-voice', 'f05-audition-a-b');
  },
  async 'f06-compare-slider'(b) {
    const p = await page(b); await hub(p, '#view=answered&item=style-frames');
    await wait(500);
    await shotEl(p, '#style-frames', 'f06-compare-slider');
  },
  async 'f07-image-viewer'(b) {
    const p = await page(b); await hub(p, '#view=answered&item=style-frames');
    await p.click('#style-frames figure.m.img img'); await wait(700);
    await shot(p, 'f07-image-viewer');
  },
  async 'f08-script-review'(b) {
    const p = await page(b); await hub(p, '#item=script-v3&doc=docs/film/harbor_lights_v3.screen.md');
    await wait(1200);
    const f = p.frames().find(fr => /harbor_lights_v3/.test(fr.url()));
    if (f) { await f.evaluate(() => { const m = document.querySelector('.marker'); if (m) m.click(); }); await wait(400); }
    await shot(p, 'f08-script-review');
  },
  async 'f09-done-view'(b) { const p = await page(b); await hub(p, '#view=closed'); await shot(p, 'f09-done-view'); },
  async 'v08-new-version-while-writing'(b) {
    // the person is typing a note on v3 when v4 is published: the hub says so at once
    const p = await page(b); await hub(p, '#item=script-v3');
    await p.click('#script-v3 > .ctl textarea'); await p.keyboard.type('The storm scene still feels long to me');
    const cp = require('child_process'), fs = require('fs'), root = process.env.DEMO_ROOT;
    fs.writeFileSync(root + '/docs/film/harbor_lights_v4.screen.md', fs.readFileSync(root + '/docs/film/harbor_lights_v3.screen.md', 'utf8').replace('Same time tomorrow?', "I'll be here tomorrow."));
    cp.execFileSync(process.env.PYTHON || 'python3', ['-I', root + '/tools/buildlog/review/review.py', '--state-dir', process.env.DEMO_STATE, 'add', '--id', 'script-v4', '--kind', 'approve',
      '--title', 'Script: Harbor Lights', '--summary', 'v4: the promise ending.', '--doc', 'docs/film/harbor_lights_v4.screen.md::Harbor Lights script', '--tag', 'script', '--supersedes', 'script-v3'], {cwd: root});
    await wait(17000);
    await shot(p, 'v08-new-version-while-writing');
  },
  async 'f10-phone'(b) { const p = await page(b, {w: 390, h: 844}); await hub(p, '#item=script-v3'); await shot(p, 'f10-phone'); },
};

(async () => {
  const b = await puppeteer.launch({executablePath: process.env.CHROME || 'chrome', headless: 'new',
    args: ['--autoplay-policy=no-user-gesture-required', '--hide-scrollbars']});
  for (const [name, fn] of Object.entries(scenes)) {
    if (only && !only.some(o => name.startsWith(o))) continue;
    try { await fn(b); } catch (e) { console.log('FAILED', name, e.message); }
  }
  await b.close();
})();
