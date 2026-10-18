// Records the README footage from a running terminal: frames per scene, assembled by make_gifs.py.
// A function of a Playwright page, so it runs from scripts/record.mjs (CI) or any Playwright harness.
async (page) => {
  const env = globalThis.process?.env ?? {};
  const OUT = env.FRAMES_DIR || 'docs/media/frames';
  const BASE = env.PORTDB_URL || 'http://localhost:5173';
  await page.setViewportSize({ width: 1100, height: 620 });
  await page.goto(`${BASE}/?q=2`);
  await page.waitForSelector('.inspector input', { timeout: 60000 });
  await page.waitForTimeout(6000);
  const api = (p, method = 'GET') => page.evaluate(async ([p, m]) => (await fetch(p, { method: m })).json(), [p, method]);
  const speed = (x) => api(`/api/sim/speed?x=${x}`, 'POST');
  const simT = async () => (await api('/api/stats')).t;
  const hud = (show) => page.evaluate((s) => { document.querySelector('.hud').style.display = s ? '' : 'none'; }, show);
  const click = (label) => page.evaluate((l) => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === l)?.click(), label);
  const focus = async (label, wheel = 0, at = [0.5, 0.5]) => {
    await click(label);
    await page.waitForTimeout(2500);
    const box = await page.locator('canvas').boundingBox();
    await page.mouse.move(box.x + box.width * at[0], box.y + box.height * at[1]);
    for (let k = 0; k < wheel; k++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(80); }
    await page.waitForTimeout(1500);
  };
  const until = async (pred, max = 240) => {
    for (let k = 0; k < max; k++) { if (await pred()) return true; await page.waitForTimeout(1000); }
    return false;
  };
  const grab = async (name, n, slow = 0.25) => {
    await speed(slow);
    await page.waitForTimeout(400);
    for (let k = 0; k < n; k++) await page.screenshot({ path: `${OUT}/${name}/${String(k).padStart(4, '0')}.png` });
    await speed(2);
  };
  const log = [];
  await speed(2);
  await hud(false);

  // daylight scenes first: the shift starts at 06:30 and the sun sets around t=540
  await focus('Yard');
  await grab('hero', 48);
  log.push('hero');

  await until(async () => (await api('/api/stats')).vessels.some((v) => v.berth === 3 && v.status === 'BERTHED'));
  await focus('Berth 3', 6, [0.5, 0.45]);
  await grab('closeup', 48);
  log.push('closeup');

  await focus('Berths 1–2', 1);
  await click('X-ray');
  await page.waitForTimeout(2000);
  await grab('xray', 40);
  await click('X-ray');
  log.push('xray');

  await focus('Yard', 2);
  const r = await until(async () => (await page.locator('.badge-rehandle:not([hidden])').count()) > 0, 300);
  await grab('rehandle', 40);
  log.push(`rehandle ${r}`);

  const t = await until(async () => ['LOADING', 'UNLOADING'].includes((await api('/api/stats')).train?.status), 300);
  await focus('Rail', 2);
  await grab('rail', 30);
  const s = await until(async () => (await api('/api/stats')).scanning > 0, 300);
  await focus('Customs', 1);
  await grab('customs', 24);
  log.push(`rail ${t} customs ${s}`);

  // time travel, HUD on: drag the scrubber back two minutes and let it play
  await hud(true);
  await focus('Overview');
  const now = await simT();
  await page.evaluate((t) => window.portdb?.seek(t), Math.max(5, now - 120));
  await page.waitForTimeout(2500);
  await grab('replay', 40, 1);
  await page.evaluate(() => window.portdb?.goLive());
  await page.waitForTimeout(3000);
  await hud(false);
  log.push('replay');

  // dusk: a storm front rolls in, the database holds every crane above its wind limit
  await speed(8);
  await until(async () => (await simT()) > 600, 400);
  await speed(2);
  await api('/api/chaos/storm', 'POST');
  await until(async () => (await api('/api/stats')).weather.wind > 21, 120);
  await focus('Berths 1–2');
  await grab('storm', 48);
  log.push('storm');

  await speed(1);
  return log;
}
