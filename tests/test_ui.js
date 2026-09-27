'use strict';
/*
 * 文件夹挑选器 —— 前端界面自动化测试（DOM 级）
 *
 * 用 jsdom 真跑 app/www/index.html 里的那份脚本，fetch 打到真实本地服务，
 * 逐条验证用户明确要求的功能：
 *   · 批量展示每一层子文件夹
 *   · 默认不展示文件（有开关）
 *   · 手动勾选子文件夹
 *   · 一键勾选第 1 层 / 第 2 层 … 的第 N 层子文件夹
 *
 * 用法：先启动后端（--host 127.0.0.1 --port 18888 --root <演示目录>），再
 *   node tests/test_ui.js
 * 依赖 jsdom（隔离工作区里，不随 fpk 分发）。
 */
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.error('缺少 jsdom，先安装：npm install jsdom（本机测用，不影响 fpk 打包）');
  process.exit(3);
}

const ORIGIN = process.env.FP_ORIGIN || 'http://127.0.0.1:18888';
const ROOT = process.env.FP_ROOT || 'C:/Users/20847/AppData/Local/Temp/fpdemo/Downloads';
const INDEX = path.resolve(__dirname, '..', 'app', 'www', 'index.html');

let pass = 0;
const fails = [];
function check(name, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a === b) { pass++; console.log('  \u2705 ' + name); }
  else { fails.push(`${name} — 得到 ${a} / 期望 ${b}`); console.log('  \u274c ' + name + '   得到 ' + a + ' / 期望 ' + b); }
}
function checkTrue(name, cond, extra) {
  check(name, !!cond, true);
  if (!cond && extra !== undefined) console.log('      extra: ' + JSON.stringify(extra));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sorted = (a) => a.slice().sort();
function stage(t) { console.log('\n== ' + t + ' =='); }

async function buildDom(pathname = '/', opts = {}) {
  const html = opts.html || fs.readFileSync(INDEX, 'utf8');
  const dom = new JSDOM(html, {
    url: opts.url || (ORIGIN + pathname),
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      // 模拟"上次留下的本地存储"：把上一份窗口里的内容灌进来，就等于用户关掉再打开
      if (opts.storage) {
        Object.keys(opts.storage).forEach((k) => {
          try { window.localStorage.setItem(k, opts.storage[k]); } catch (e) { /* 忽略 */ }
        });
      }
      // 模拟被沙箱限制的窗口：连读 localStorage 都抛 SecurityError
      if (opts.blockStorage) {
        Object.defineProperty(window, 'localStorage', {
          configurable: true,
          get() { throw new window.DOMException('denied by sandbox', 'SecurityError'); },
        });
      }
      window.fetch = opts.fetch
        ? (u, o) => opts.fetch(String(u), o)
        : (u, opt) => fetch(new URL(String(u), ORIGIN + '/').href, opt);
      window.confirm = () => true;
      window.alert = () => {};
      if (!window.URL.createObjectURL) window.URL.createObjectURL = () => 'blob:test';
      if (!window.URL.revokeObjectURL) window.URL.revokeObjectURL = () => {};
    },
  });
  const win = dom.window;
  win.onerror = (msg) => fails.push('页面脚本错误: ' + msg);
  return dom;
}

/* 把某个窗口的本地存储原样抄下来，用来给下一个窗口"接着上次用" */
function snapshot(win) {
  const out = {};
  try {
    for (let i = 0; i < win.localStorage.length; i++) {
      const k = win.localStorage.key(i);
      out[k] = win.localStorage.getItem(k);
    }
  } catch (e) { /* 拿不到就算了 */ }
  return out;
}

async function waitFor(fn, label, timeout = 15000) {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < timeout) {
    try { last = fn(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(120);
  }
  throw new Error('等待超时: ' + label + '（最后一次：' + last + '）');
}

function helpers(dom) {
  const win = dom.window;
  const d = win.document;
  const $ = (id) => d.getElementById(id);
  const text = (sel) => Array.from(d.querySelectorAll(sel)).map((e) => e.textContent.trim());
  const setInput = (id, v) => {
    const el = $(id);
    el.value = v;
    el.dispatchEvent(new win.Event('input', { bubbles: true }));
  };
  const toggleChk = (id, v) => {
    const el = $(id);
    el.checked = v;
    el.dispatchEvent(new win.Event('change', { bubbles: true }));
  };
  const setSelect = (id, v) => {
    const el = $(id);
    el.value = v;
    el.dispatchEvent(new win.Event('change', { bubbles: true }));
  };
  const names = () => text('#tree .node:not(.file) .nm');
  const fileNames = () => text('#tree .node.file .nm');
  const fileMetas = () => text('#tree .node.file .meta');
  const selCount = () => Number($('selCount').textContent);
  const selItems = () => text('#selList div').sort();
  const clickLevel = (label, btn) => {
    const lv = Array.from(d.querySelectorAll('#levels .lv'))
      .find((e) => e.querySelector('b').textContent === label);
    if (!lv) throw new Error('找不到层按钮 ' + label);
    const b = Array.from(lv.querySelectorAll('button')).find((x) => x.textContent === btn);
    if (!b) throw new Error('找不到 ' + label + ' 上的 ' + btn);
    b.click();
  };
  const expandAll = () => win.eval('expandAll(true)');
  // 重读结构 + 等过防抖窗口 + 全部展开，避免读到中间态
  const reload = async () => {
    await win.eval('loadTree()');
    await sleep(420);
    win.eval('expandAll(true)');
    await sleep(140);
  };
  return { win, d, $, text, setInput, toggleChk, setSelect, names, fileNames, fileMetas,
           selCount, selItems, clickLevel, expandAll, reload };
}

/* ------------------------------------------------------------------ */
/* 这套用例是"破坏性"的——它会真的把文件夹搬进目标目录。
   所以每次开跑前把演示数据重建一遍，否则第二次跑必然一堆失败。
   想保留现场就设 FP_NO_RESET=1。 */
const DEMO_DIR = path.dirname(ROOT);        // 后端白名单根（含 Downloads / media）
function resetDemoData() {
  const dirs = [
    'Downloads/包A/乱七八糟/ShowA.S01/Subs',
    'Downloads/包A/乱七八糟/Sample',
    'Downloads/包B/ShowB.S02',
    'media/剧集',
  ];
  dirs.forEach((d) => fs.rmSync(path.join(DEMO_DIR, d.split('/')[0]),
    { recursive: true, force: true }));
  dirs.forEach((d) => fs.mkdirSync(path.join(DEMO_DIR, d), { recursive: true }));
  const files = {
    'Downloads/包A/乱七八糟/ShowA.S01/ShowA.S01E01.mkv': 1048576,
    'Downloads/包A/乱七八糟/ShowA.S01/ShowA.S01E02.mkv': 2097152,
    'Downloads/包A/乱七八糟/ShowA.S01/Subs/chs.ass': 1,
    'Downloads/包A/乱七八糟/Sample/sample.mkv': 4096,
    'Downloads/包B/ShowB.S02/ShowB.S02E01.mkv': 524288,
    'Downloads/广告.txt': 8,
  };
  Object.keys(files).forEach((f) => {
    fs.writeFileSync(path.join(DEMO_DIR, f), Buffer.alloc(files[f], 120));
  });
  console.log('演示数据已重建：' + DEMO_DIR);
}

async function run() {
  if (!process.env.FP_NO_RESET) resetDemoData();
  const dom = await buildDom('/');
  const h = helpers(dom);

  stage('页面加载与基础信息');
  await waitFor(() => h.$('modeBadge').textContent !== '加载中…', 'boot 完成');
  checkTrue('连接方式标记已显示',
    ['统一网关', 'TCP 直连'].includes(h.$('modeBadge').textContent), h.$('modeBadge').textContent);
  check('版本号已显示', h.$('verBadge').textContent, 'v1.4.1');
  checkTrue('页面自带「按层一键勾选」区', /按层一键勾选/.test(h.d.body.textContent));
  check('默认「显示文件」是关闭的', h.$('showFiles').checked, false);
  check('展开层数默认 3 层', h.$('depth').value, '3');

  /* ---- 改版要求一：三块合并成一块，不再是好几个小窗口 ---- */
  stage('界面合并：按层勾选 + 已勾选 + 操作 合成一个「工作台」');
  const work = h.d.querySelector('.card.work');
  const inWork = (id) => !!h.d.querySelector('.card.work #' + id);
  checkTrue('页面上有「工作台」这一块', !!work);
  checkTrue('「按层一键勾选」在工作台里', !!(work && /按层一键勾选/.test(work.textContent)));
  checkTrue('「已勾选」也在同一个工作台里（不再是独立小窗）',
    inWork('selCount') && inWork('selFiles') && inWork('selSize') && inWork('selList'));
  checkTrue('「操作」也在同一个工作台里',
    inWork('destPath') && inWork('conflict') && inWork('keepStructure') && inWork('jobBox'));
  checkTrue('目录树在工作台外面，单独一块',
    !!h.d.querySelector('.treecard #tree') && !inWork('tree'));
  // 合并前是「来源与筛选 / 按层勾选 / 已勾选 / 操作 / 目录结构」五块
  check('页面上只剩三块卡片（工具条 / 工作台 / 目录树）',
    h.d.querySelectorAll('.wrap .card').length, 3);
  checkTrue('工作台排在目录树前面（左栏）',
    !!(work.compareDocumentPosition(h.$('tree')) & 4));

  /* ---- 改版要求二：紧凑 ---- */
  stage('紧凑：按层列表一行一层、按钮统一小一号、筛选默认收起');
  const lvRows = Array.from(h.d.querySelectorAll('#levels .lv'));
  checkTrue('按层勾选排成整齐的一行一层', lvRows.length > 0 &&
    lvRows.every((e) => e.querySelector('b')), lvRows.length);
  checkTrue('按层按钮全部是 mini 小尺寸',
    Array.from(work.querySelectorAll('#levels button')).every((b) => b.classList.contains('mini')));
  check('筛选条件默认收起（界面上默认只占一行）',
    h.$('advRow').classList.contains('on'), false);
  h.win.eval('toggleAdv()');
  await sleep(80);
  check('点了才展开筛选条件', h.$('advRow').classList.contains('on'), true);
  checkTrue('按钮文案跟着变成「收起」', /▴/.test(h.$('advBtn').textContent), h.$('advBtn').textContent);
  h.win.eval('toggleAdv(false)');
  await sleep(80);
  check('再点一次收回去', h.$('advRow').classList.contains('on'), false);
  // 收起后条件依然生效，所以按钮上要能看出"已经设了几项"，别让人以为没筛选
  h.setInput('filterText', 'S01');
  await sleep(80);
  checkTrue('收起状态下按钮标出已设的条件数',
    /筛选条件 1 项/.test(h.$('advBtn').textContent), h.$('advBtn').textContent);
  h.setInput('filterText', '');
  await sleep(80);
  checkTrue('条件清掉后按钮恢复原样',
    /^筛选条件 ▾$/.test(h.$('advBtn').textContent), h.$('advBtn').textContent);

  /* ---- 默认聚合视图：不填来源目录时，直接把各存储空间里的文件夹并成一个列表 ---- */
  stage('默认聚合视图（不必从存储空间开始选）');
  await waitFor(() => h.d.querySelectorAll('#tree .node').length > 0, '默认聚合树渲染');
  check('来源留空 = 聚合视图，根节点是虚拟的「全部存储空间」', h.names()[0], '全部存储空间');
  h.expandAll();
  await sleep(220);
  // 展开所有层后再按「第 1 层」筛，确认聚合层没有多出"存储空间 / 1000"这两级
  const lv1 = Array.from(h.d.querySelectorAll('#tree .node:not(.file)'))
    .filter((n) => /^第 1 层/.test(n.querySelector('.meta').textContent))
    .map((n) => n.querySelector('.nm').textContent);
  check('第 1 层直接是各存储空间里的文件夹（没有"存储空间 → 1000"这两级）',
    sorted(lv1), sorted(['Downloads', 'media']));
  checkTrue('页面标出聚合了几个存储空间',
    /已聚合 \d+ 个存储空间/.test(h.$('aggBadge').textContent), h.$('aggBadge').textContent);
  check('聚合根不可勾选（它是视图，不是真实目录）',
    h.d.querySelector('#tree .node input[type=checkbox]').disabled, true);
  check('按层快捷勾选从第 1 层开始（虚拟根不占层）', h.text('#levels .lv b')[0], '第 1 层');
  check('第 1 层就是那 2 个文件夹',
    h.text('#tree .node:not(.file) .meta').filter((t) => t.startsWith('第 1 层')).length, 2);
  checkTrue('统计文案说明了聚合范围',
    /^聚合 \d+ 个存储空间/.test(h.$('treeStat').textContent), h.$('treeStat').textContent);

  stage('读取目录结构：批量展示每一层子文件夹');
  h.setInput('rootPath', ROOT);
  h.setSelect('depth', '3');
  await h.win.eval('loadTree()');
  await waitFor(() => h.d.querySelectorAll('#tree .node').length > 0, '目录树渲染');
  await h.reload();

  check('展开后 6 个文件夹（覆盖 3 层，第 2 层还有个 ShowB.S02）',
    sorted(h.names().slice(1)),
    sorted(['Sample', 'ShowA.S01', 'ShowB.S02', '包A', '包B', '乱七八糟']));
  check('第 1 层子文件夹',
    h.text('#tree .node:not(.file) .meta').filter((t) => t.startsWith('第 1 层')).length, 2);
  checkTrue('每行标注了所属层级',
    h.text('#tree .node:not(.file) .meta').every((t) => /^第 \d+ 层/.test(t)),
    h.text('#tree .node:not(.file) .meta'));
  check('第 3 层的两个文件夹都在',
    h.text('#tree .node:not(.file) .meta').filter((t) => t.startsWith('第 3 层')).length, 2);
  checkTrue('顶部统计给出文件夹总数与最大层数',
    /共 \d+ 个文件夹，最深 \d+ 层/.test(h.$('treeStat').textContent), h.$('treeStat').textContent);

  stage('筛选条件：不展示文件（默认）/ 需要时才展示');
  check('默认一个文件都不显示', h.d.querySelectorAll('#tree .node.file').length, 0);
  check('文件行数不含在文件夹统计里', h.d.querySelectorAll('#tree .node:not(.file)').length, 7); // 根 + 6

  h.toggleChk('showFiles', true);
  await waitFor(() => h.d.querySelectorAll('#tree .node.file').length > 0, '文件行出现');
  h.expandAll();
  await sleep(200);
  checkTrue('能看到 广告.txt', h.fileNames().includes('广告.txt'), h.fileNames());
  check('文件行的勾选框全部禁用（文件不能单独搬运）',
    Array.from(h.d.querySelectorAll('#tree .node.file input[type=checkbox]')).every((cb) => cb.disabled), true);
  checkTrue('视频文件标注了「视频」', h.fileMetas().some((t) => /视频/.test(t)), h.fileMetas());
  checkTrue('文件行显示大小', h.fileMetas().every((t) => /(B|KB|MB|GB)/.test(t)), h.fileMetas());
  check('文件不参与按层全选（层统计只有 3 层）',
    h.text('#levels .lv b').filter((t) => t.startsWith('第')).length, 3);

  h.toggleChk('showFiles', false);
  await waitFor(() => h.d.querySelectorAll('#tree .node.file').length === 0, '文件行隐藏');
  check('关掉开关后文件重新隐藏', h.d.querySelectorAll('#tree .node.file').length, 0);

  stage('名称筛选');
  h.setInput('filterText', 'S01');
  await h.reload();
  check('筛选 S01 时保留整条路径，只留命中分支',
    sorted(h.names().slice(1)), sorted(['包A', '乱七八糟', 'ShowA.S01']));

  // 只改开关、不点「应用筛选」，验证防抖自动刷新不会串台（曾经的竞态 bug）
  h.toggleChk('showFiles', true);
  await h.reload();
  check('自动刷新后筛选条件依然生效（没有回退成全量）',
    sorted(h.names().slice(1)), sorted(['包A', '乱七八糟', 'ShowA.S01']));
  checkTrue('筛选同时作用于文件名', h.fileNames().every((n) => /S01/i.test(n)), h.fileNames());

  h.toggleChk('showFiles', false);
  h.setInput('filterText', '');
  await h.reload();
  check('清空筛选后恢复全量', h.d.querySelectorAll('#tree .node:not(.file)').length, 7);

  stage('一键勾选第 N 层子文件夹');
  check('每一层都有「全选 / 取消」，外加「全部层」',
    h.text('#levels .lv b'), ['第 1 层', '第 2 层', '第 3 层', '全部层']);
  checkTrue('层按钮上带该层文件夹数量',
    /第1层2个.*第2层2个.*第3层2个/.test(h.$('levels').textContent.replace(/\s+/g, '')),
    h.$('levels').textContent.replace(/\s+/g, ''));

  h.clickLevel('第 1 层', '全选');
  await sleep(150);
  check('一键勾第 1 层 = 2 个', h.selCount(), 2);
  check('清单里正好是第 1 层的两个', h.selItems(), sorted(['第1层 · 包A', '第1层 · 包B']));
  check('被上级涵盖的子节点置灰，避免重复搬运（剩下 4 个）',
    h.d.querySelectorAll('#tree .node.implied').length, 4);
  check('被勾中的行被高亮（不含被上级涵盖的）',
    h.d.querySelectorAll('#tree .node.sel:not(.implied)').length, 2);

  h.clickLevel('第 3 层', '全选');
  await sleep(150);
  check('已有上级选中时，再勾第 3 层不会重复计数', h.selCount(), 2);

  h.clickLevel('第 1 层', '取消');
  await sleep(150);
  check('取消第 1 层后清空', h.selCount(), 0);

  h.clickLevel('第 2 层', '全选');
  await sleep(150);
  check('一键勾第 2 层 = 2 个（乱七八糟 / ShowB.S02）', h.selCount(), 2);
  check('清单里是第 2 层的两个',
    h.selItems(), sorted(['第2层 · 乱七八糟', '第2层 · ShowB.S02']));

  h.clickLevel('第 2 层', '取消');
  await sleep(150);
  h.clickLevel('第 3 层', '全选');
  await sleep(150);
  check('一键勾第 3 层 = 2 个（Sample / ShowA.S01）', h.selCount(), 2);
  check('清单内容正确', h.selItems(), sorted(['第3层 · Sample', '第3层 · ShowA.S01']));

  stage('手动勾选 / 反选 / 清空');
  h.win.eval('clearSelection()');
  h.expandAll();
  await sleep(150);
  const pick = (name) => Array.from(h.d.querySelectorAll('#tree .node:not(.file)'))
    .find((n) => n.querySelector('.nm').textContent === name);
  const box = (name) => pick(name).querySelector('input[type=checkbox]');
  check('未被上级涵盖时复选框可用', box('包A').disabled, false);
  box('ShowA.S01').checked = true;
  box('ShowA.S01').dispatchEvent(new h.win.Event('change'));
  await sleep(150);
  check('手动勾选单个文件夹生效', h.selCount(), 1);
  check('勾选后该行被高亮', h.d.querySelectorAll('#tree .node.sel').length, 1);
  box('Sample').checked = true;
  box('Sample').dispatchEvent(new h.win.Event('change'));
  await sleep(150);
  check('手动勾多个累计', h.selCount(), 2);
  box('ShowA.S01').checked = false;
  box('ShowA.S01').dispatchEvent(new h.win.Event('change'));
  await sleep(150);
  check('取消勾选减一', h.selCount(), 1);

  const allLv = Array.from(h.d.querySelectorAll('#levels .lv'))
    .find((e) => e.querySelector('b').textContent === '全部层');
  allLv.querySelectorAll('button')[1].click();
  await sleep(150);
  check('反选：6 个文件夹里剩 5 个', h.selCount(), 5);
  h.win.eval('clearSelection()');
  await sleep(100);
  check('清空选择', h.selCount(), 0);

  /* ---- 改版要求三：文件夹很多时，靠"点清单定位"少滚几屏 ---- */
  stage('已勾选清单 → 在目录树里点击定位');
  h.clickLevel('第 3 层', '全选');
  await sleep(150);
  h.win.eval('expandAll(false)');      // 全部收起，模拟"树很长"时的状态
  await sleep(150);
  check('全部收起后，深层的 Sample 不在 DOM 里', h.names().includes('Sample'), false);

  const item = Array.from(h.d.querySelectorAll('#selList div[data-p]'))
    .find((e) => /Sample/.test(e.textContent));
  checkTrue('清单条目可点击定位', !!item, h.d.querySelectorAll('#selList div').length);
  item.click();
  await sleep(200);
  check('点条目后树里出现高亮定位', h.d.querySelectorAll('#tree .node.flash').length, 1);
  checkTrue('定位时自动展开了祖先链，深层节点看得见', h.names().includes('Sample'), h.names());
  h.expandAll();
  await sleep(150);

  stage('计算大小 / 预演 / 移动');
  h.clickLevel('第 3 层', '全选');
  await sleep(150);
  await h.win.eval('calcStats()');
  await waitFor(() => h.$('selFiles').textContent !== '—', '统计返回');
  checkTrue('统计出文件数', /^\d[\d,]*$/.test(h.$('selFiles').textContent), h.$('selFiles').textContent);
  checkTrue('统计出总大小', /(B|KB|MB|GB)/.test(h.$('selSize').textContent), h.$('selSize').textContent);

  h.setInput('destPath', ROOT.replace(/\/Downloads$/, '') + '/media');
  await h.win.eval("runJob('plan')");
  await waitFor(() => h.$('jobLog').textContent.includes('预演结果') || h.$('jobText').querySelector('.err'),
    '预演结果');
  const plan = h.$('jobLog').textContent;
  if (h.$('jobText').querySelector('.err')) {
    console.log('      jobText: ' + h.$('jobText').textContent);
  }
  checkTrue('预演列出待处理项', /ShowA\.S01/.test(plan) && /Sample/.test(plan), plan);
  checkTrue('预演明确声明没有动文件', /没有动任何文件/.test(plan), plan.slice(0, 100));
  // 每项渲染成「源」+「目标」两行，所以按 <div> 项数统计，不能数箭头个数
  const planHtml = h.$('jobLog').innerHTML;
  const planItems = (planHtml.match(/<div>(?:→ |<span class="err">跳过<\/span> )/g) || []).length;
  check('预演只列出选中的 2 项', planItems, 2);
  check('每项都画出了「源 → 目标」', (planHtml.match(/→ /g) || []).length, 4);

  h.win.eval("runJob('move')");   // 不能 await：会停在自绘确认框上等点击
  await waitFor(() => h.d.querySelector('#confirmBox.on'), '确认框弹出');
  checkTrue('移动前弹出的是页面自绘确认框（iframe 里原生 confirm 会被拦）',
    /移动到/.test(h.$('cfBody').textContent), h.$('cfBody').textContent);
  h.d.querySelector('#confirmBox .ft .primary').click();
  await waitFor(() => /完成|失败|错误|取消/.test(h.$('jobText').textContent), '移动结束', 40000);
  checkTrue('移动任务报告完成', /完成/.test(h.$('jobText').textContent), h.$('jobText').textContent);
  const n = (h.$('jobText').textContent.match(/完成：(\d+) 个文件/) || [])[1];
  checkTrue('确实搬走了文件', Number(n) > 0, h.$('jobText').textContent);

  stage('路径安全');
  h.setInput('rootPath', 'C:/Windows');
  await h.win.eval('loadTree()');
  await waitFor(() => /err/.test(h.d.querySelector('#tree').innerHTML), '越权被拒');
  checkTrue('白名单外的目录被拒绝',
    /权限/.test(h.d.querySelector('#tree').textContent), h.d.querySelector('#tree').textContent);

  // 用户反馈过「目录浏览打不开」：起点是 "/"，不在白名单里，弹窗永远加载中。
  // 现在起点更进一步，是「聚合层」——各存储空间用户目录下的文件夹并成一个列表。
  stage('目录选择器：起点是聚合层，不是文件系统根');
  h.setInput('rootPath', '');
  h.win.eval('openPicker("root")');
  await waitFor(() => h.$('pkList').querySelectorAll('[data-p]').length > 0, '聚合目录列表');
  checkTrue('没有卡在「加载中」', !/读取中/.test(h.$('pkList').textContent),
    h.$('pkList').textContent.slice(0, 80));
  checkTrue('一进来就列出各存储空间里的文件夹（不必先点存储空间再点 1000）',
    /Downloads/.test(h.$('pkList').textContent), h.$('pkList').textContent.slice(0, 120));
  check('面包屑首级是「全部存储空间」', h.$('pkCrumb').textContent.trim(), '全部存储空间');

  // 想按盘逐级看时，用「按存储空间」切到存储空间列表
  h.win.eval('pkGo("/")');
  await waitFor(() => /fpdemo/.test(h.$('pkList').textContent), '存储空间列表');
  checkTrue('「按存储空间」能列出各存储空间',
    /fpdemo/.test(h.$('pkList').textContent), h.$('pkList').textContent.slice(0, 120));
  checkTrue('存储空间列表的路径仍然可以回到聚合层',
    /全部存储空间/.test(h.$('pkCrumb').textContent), h.$('pkCrumb').textContent);

  h.$('pkList').querySelector('[data-p]').click();
  await waitFor(() => /Downloads/.test(h.$('pkList').textContent), '进入存储空间');
  checkTrue('能继续往里点', /Downloads/.test(h.$('pkList').textContent), h.$('pkList').textContent);
  checkTrue('面包屑变成「存储空间 / … / fpdemo」',
    /存储空间/.test(h.$('pkCrumb').textContent) && /fpdemo/.test(h.$('pkCrumb').textContent),
    h.$('pkCrumb').textContent);

  h.win.eval('pkUp()');
  await waitFor(() => /存储空间/.test(h.$('pkCrumb').textContent)
    && !/fpdemo/.test(h.$('pkCrumb').textContent), '上一级回存储空间列表');
  checkTrue('存储空间根再「上一级」回到存储空间列表',
    /fpdemo/.test(h.$('pkList').textContent), h.$('pkList').textContent.slice(0, 80));

  h.win.eval('pkConfirm()');
  await sleep(150);
  check('还没选目录时「选用当前目录」不会写入', h.$('rootPath').value, '');
  h.win.eval('closePicker()');

  /* ---- 回归：飞牛网关下入口是 /app/folder-picker（**没有结尾斜杠**）----
     旧实现靠"砍掉最后一个路径段"推导接口地址，会算成 /app/，
     所有请求打到 /app/api/... 而网关只转发 /app/folder-picker/**，
     表现就是用户看到的「请求失败 HTTP 404」+ 目录选择器空着。 */
  stage('统一网关：入口不带结尾斜杠（404 回归）');
  const dom2 = await buildDom('/app/folder-picker');
  const g = helpers(dom2);
  g.setInput('rootPath', ROOT);
  await waitFor(() => g.d.querySelectorAll('#tree .node').length > 0,
    '不带结尾斜杠时目录树渲染');
  check('接口根地址探测正确', g.win.eval('BASE'), '/app/folder-picker/');
  check('版本号也是新的', g.$('verBadge').textContent, 'v1.4.1');
  g.expandAll();
  await sleep(150);
  checkTrue('不带结尾斜杠也能读到子文件夹', g.names().length >= 2, g.names());

  // 用户报的第二个现象：目录选择器里一个文件夹都没有
  // （rootPath 已填，所以选择器直接落在该目录；关键是它必须列出内容而不是空着）
  g.win.eval('openPicker("root")');
  await waitFor(() => g.$('pkList').querySelectorAll('[data-p]').length > 0,
    '网关下目录列表');
  checkTrue('网关下目录选择器列出了文件夹',
    !/读取中/.test(g.$('pkList').textContent) && /包A/.test(g.$('pkList').textContent),
    g.$('pkList').textContent.slice(0, 140));
  // 一路「上一级」要能回到存储空间列表，说明导航在网关下也是通的
  for (let i = 0; i < 5; i++) { g.win.eval('pkUp()'); await sleep(260); }
  await waitFor(() => /fpdemo/.test(g.$('pkList').textContent), '网关下回到存储空间');
  checkTrue('网关下也能回到存储空间列表', /fpdemo/.test(g.$('pkList').textContent),
    g.$('pkList').textContent.slice(0, 140));
  g.win.eval('closePicker()');

  /* ---- 严格网关：真实飞牛网关只转发 /app/folder-picker/**，其它一律 404。
     这一条不依赖后端的兜底，直接钉住前端"接口地址推导"本身。 ---- */
  stage('严格网关：只转发 /app/folder-picker/**');
  const hits = [];
  const dom5 = await buildDom('/app/folder-picker', {
    fetch: async (u) => {
      hits.push(u);
      if (u.indexOf('/app/folder-picker/') !== 0) {
        return { ok: false, status: 404, json: async () => ({}) };  // 网关 404
      }
      return fetch(new URL(u, ORIGIN + '/').href, { headers: { 'X-FP-Probe': '1' } });
    },
  });
  const g5 = helpers(dom5);
  g5.setInput('rootPath', ROOT);
  await waitFor(() => g5.d.querySelectorAll('#tree .node').length > 0, '严格网关下目录树渲染');
  check('接口根地址就是网关前缀', g5.win.eval('BASE'), '/app/folder-picker/');
  check('没有一条请求跑到网关前缀外面（否则就是 404）',
    hits.filter((u) => u.indexOf('/app/folder-picker/') !== 0), []);
  checkTrue('确实请求了 /app/folder-picker/api/info',
    hits.indexOf('/app/folder-picker/api/info') > -1, hits.slice(0, 3));
  checkTrue('严格网关下目录选择器也能用',
    await (async () => {
      g5.win.eval('openPicker("root")');
      await sleep(400);
      return !/读取中/.test(g5.$('pkList').textContent);
    })(), g5.$('pkList').textContent.slice(0, 120));
  g5.win.eval('closePicker()');
  dom5.window.close();

  stage('统一网关：入口带结尾斜杠');
  const dom3 = await buildDom('/app/folder-picker/');
  const g3 = helpers(dom3);
  g3.setInput('rootPath', ROOT);
  await waitFor(() => g3.d.querySelectorAll('#tree .node').length > 0, '带斜杠时目录树渲染');
  check('带结尾斜杠时根地址正确', g3.win.eval('BASE'), '/app/folder-picker/');

  stage('统一网关：应用挂在更深的路径下');
  const dom4 = await buildDom('/some/prefix/app/folder-picker');
  const g4 = helpers(dom4);
  g4.setInput('rootPath', ROOT);
  await waitFor(() => g4.d.querySelectorAll('#tree .node').length > 0,
    '多层前缀下目录树渲染');
  check('逐级上溯后找到真实前缀', g4.win.eval('BASE'), '/some/prefix/app/folder-picker/');

  /* ---- 演示页：用户装之前会先点开它，别让它烂掉 ---- */
  stage('演示页（假数据）');
  const demoPath = path.resolve(__dirname, '..', 'preview', 'folder-picker-demo.html');
  checkTrue('演示页已生成', fs.existsSync(demoPath), demoPath);
  if (fs.existsSync(demoPath)) {
    const noNet = async () => ({ ok: false, status: 404, json: async () => ({}) });
    const domD = await buildDom('/', {
      html: fs.readFileSync(demoPath, 'utf8'),
      url: 'file:///C:/demo/folder-picker-demo.html',
      fetch: noNet,
    });
    const hd = helpers(domD);
    await waitFor(() => hd.d.querySelectorAll('#tree .node').length > 0, '演示页目录树渲染');
    check('演示页版本标记', hd.$('verBadge').textContent, 'v1.4.1（演示）');
    checkTrue('演示页能列出文件夹', hd.names().length >= 3, hd.names());
    check('演示页默认也是聚合视图，根节点是「全部存储空间」', hd.names()[0], '全部存储空间');
    hd.expandAll();
    await sleep(220);
    checkTrue('演示页第 1 层就列出各存储空间里的文件夹',
      /纪录片合集/.test(hd.names().join(' ')) && /AnotherShow/.test(hd.names().join(' ')),
      hd.names().slice(0, 10));
    checkTrue('演示页标出同名文件夹来自哪块盘',
      /同名 \d+ 处/.test(hd.d.querySelector('#tree').textContent),
      hd.d.querySelector('#tree').textContent.slice(0, 200));
    hd.win.eval('openPicker("root")');
    await waitFor(() => hd.$('pkList').querySelectorAll('[data-p]').length > 0, '演示页选择器');
    // 演示页的来源目录是空的（用聚合视图），所以选择器直接列出聚合后的文件夹
    checkTrue('演示页目录选择器列出了文件夹',
      !/读取中/.test(hd.$('pkList').textContent) &&
      /最新剧集合集/.test(hd.$('pkList').textContent),
      hd.$('pkList').textContent.slice(0, 140));
    check('演示页面包屑首级是「全部存储空间」',
      hd.$('pkCrumb').textContent.trim().split('/')[0].trim(), '全部存储空间');
    checkTrue('演示页同样是合并后的工作台布局',
      !!hd.d.querySelector('.card.work #levels') && !!hd.d.querySelector('.card.work #destPath'));
    check('演示页也是三块卡片', hd.d.querySelectorAll('.wrap .card').length, 3);
    domD.window.close();
  }

  /* ---- 用户要求：顶上那排（应用图标 + 应用名 + 副标题）不要了 ---- */
  stage('页头那一排已移除');
  check('页面上不再有页头整行', h.d.querySelectorAll('header').length, 0);
  checkTrue('应用名/副标题那排文案已消失（小窗标题栏上本来就有）',
    !/看清每一层子文件夹/.test(h.d.body.textContent));
  checkTrue('网关 / 版本标记挪到页脚，仍然看得到',
    !!h.d.querySelector('.foot #modeBadge') && !!h.d.querySelector('.foot #verBadge'));
  checkTrue('页脚说明了「会记住上次的条件」',
    /会自动记住/.test(h.$('rememberHint').textContent), h.$('rememberHint').textContent);

  /* ---- 用户要求：记住上次的筛选和来源目录 ---- */
  stage('记住上次的筛选条件（关掉再打开还在）');
  const p1 = await buildDom('/');
  const k1 = helpers(p1);
  await waitFor(() => k1.$('modeBadge').textContent !== '加载中…', '记忆用例 boot');
  k1.setInput('filterText', 'Show');
  k1.setInput('excludeText', 'Sample');
  k1.toggleChk('showFiles', true);
  k1.setSelect('depth', '5');
  k1.win.eval('toggleAdv(true)');
  await sleep(500);                       // 等防抖落盘
  const prefs1 = JSON.parse(p1.window.localStorage.getItem('folder-picker.prefs.v1'));
  check('筛选关键词被记住', prefs1.filterText, 'Show');
  check('排除词被记住', prefs1.excludeText, 'Sample');
  check('「显示文件」开关被记住', prefs1.showFiles, true);
  check('展开层数被记住', prefs1.depth, '5');
  check('筛选区展开状态被记住', prefs1.adv, true);
  check('没动过的开关保持默认（显示空文件夹仍是关的）', prefs1.showEmpty, false);

  stage('记住上次的来源目录（含「最近用过」列表）');
  k1.setInput('rootPath', ROOT);
  await k1.win.eval('loadTree()');
  await sleep(250);
  const hist1 = JSON.parse(p1.window.localStorage.getItem('folder-picker.roots.v1'));
  check('读成功的来源目录进了历史', hist1[0], ROOT);
  check('历史里没有重复项', hist1.filter((x) => x === ROOT).length, 1);
  await k1.win.eval('loadTree()');
  await sleep(250);
  check('重复读同一个目录不会记第二遍',
    JSON.parse(p1.window.localStorage.getItem('folder-picker.roots.v1')).length, 1);
  const bogus = DEMO_DIR + '/nope-not-here';
  k1.setInput('rootPath', bogus);
  await k1.win.eval('loadTree()');
  await sleep(250);
  checkTrue('读失败的路径不会被记住（打错的目录别下次还弹回来）',
    JSON.parse(p1.window.localStorage.getItem('folder-picker.roots.v1')).indexOf(bogus) === -1,
    p1.window.localStorage.getItem('folder-picker.roots.v1'));
  check('上次成功那个仍是「上次用的来源目录」',
    JSON.parse(p1.window.localStorage.getItem('folder-picker.prefs.v1')).lastRoot, ROOT);

  stage('重开页面：筛选条件与来源目录自动填回');
  const p2 = await buildDom('/', { storage: snapshot(p1.window) });
  const k2 = helpers(p2);
  await waitFor(() => k2.$('modeBadge').textContent !== '加载中…', '重开页 boot');
  check('重开后筛选关键词自动填回', k2.$('filterText').value, 'Show');
  check('重开后排除词自动填回', k2.$('excludeText').value, 'Sample');
  check('重开后「显示文件」还是开着的', k2.$('showFiles').checked, true);
  check('重开后展开层数还是 5', k2.$('depth').value, '5');
  check('重开后筛选区仍是展开的', k2.$('advRow').classList.contains('on'), true);
  check('重开后来源目录自动填回上次那个', k2.$('rootPath').value, ROOT);
  checkTrue('历史也进了输入框的下拉候选（点着就能换）',
    Array.from(k2.d.querySelectorAll('#rootHist option')).map((o) => o.value).includes(ROOT));
  k2.win.eval('openPicker("root")');
  await waitFor(() => k2.$('pkList').querySelectorAll('[data-p]').length > 0, '重开页选择器');
  checkTrue('目录选择器顶部给出「最近用过」',
    /最近用过/.test(k2.$('pkRecent').textContent), k2.$('pkRecent').textContent);
  check('「最近用过」第一条就是上次用的来源目录',
    k2.$('pkRecent').querySelector('.chip').getAttribute('data-p'), ROOT);
  k2.win.eval('closePicker()');

  stage('窗口禁用本地存储时：不崩，并如实提示');
  const p3 = await buildDom('/', { blockStorage: true });
  const k3 = helpers(p3);
  await waitFor(() => k3.$('modeBadge').textContent !== '加载中…', '禁用存储时也能 boot');
  await waitFor(() => k3.d.querySelectorAll('#tree .node').length > 0, '禁用存储时目录树照常渲染');
  checkTrue('页脚如实说明「不会被记住」',
    /不会被记住/.test(k3.$('rememberHint').textContent), k3.$('rememberHint').textContent);
  let threw = '';
  try {
    k3.win.eval('savePrefs()');
    k3.setInput('filterText', 'Show');
    await sleep(220);
  } catch (e) { threw = String((e && e.message) || e); }
  check('本地存储不可用时读写都不抛异常', threw, '');
  await k3.win.eval('loadTree()');
  await sleep(300);
  checkTrue('禁用存储也不影响正常使用（树照旧读出来）',
    k3.d.querySelectorAll('#tree .node').length > 0);
  p1.window.close();
  p2.window.close();
  p3.window.close();

  stage('页面报错检查');
  check('没有未捕获的脚本错误', fails.filter((f) => f.startsWith('页面脚本错误')), []);

  dom.window.close();
  dom2.window.close();
  dom3.window.close();
  dom4.window.close();
}

(async () => {
  try {
    await run();
  } catch (e) {
    fails.push('测试异常：' + ((e && e.message) || e));
    console.error('\n' + ((e && e.stack) || e));
  }
  console.log('\n' + '-'.repeat(52));
  if (fails.length) console.log(fails.map((f) => '  \u274c ' + f).join('\n'));
  console.log(`通过 ${pass} / ${pass + fails.length}`);
  process.exit(fails.length ? 1 : 0);
})();
