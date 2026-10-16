"""对话窗口 —— 单文件、无构建步骤，由 server 直接吐出来。

这个页面的目标是**纯净**：一条对话流 + 一个输入框，仅此而已。
没有 arm 选择、没有模式开关、没有指标行、没有约束 chip、没有审计面板、
没有"试跑 N 轮"、没有"看成片" —— 那些全是实验台的东西，属于 CLI 与接口，
不属于"跟模型说一句话"这件事。

三条硬性质（都由服务端保证，前端只是不把它们画出来）：

1. **每句话就是一轮**：发送走 `POST /session/{id}/say` —— 这句话落在 prompt 骨架里那个
   固定的「本轮任务」段上，然后照常跑完一整轮（落盘、验证、ΔC、判定、提交）。
   窗口里看到的是这一轮的**正文**（`turn_view` 把 O_i 的叙述切出来）；
   `file:` 块和它们的正文不进窗口 —— 那些正是被压缩成事实链的对象。
   上下文里没有"窗口里那串历史"：每轮只给 P + C + 这一句话。
2. **思维链不可见**：回复里只有正文。连"隐藏了多少字思考"都不显示。
   `content` 为空而 `reasoning_content` 非空时，客户端会把思考当正文回退 ——
   服务端把那条唯一的泄漏路径改写成"这轮没写正文"（见 `Session.say`）。
3. **已渲染的内容不可变**：说过的每一句都追加进 `chat.jsonl`（append-only，不入库），
   刷新页面原样摆回来，不会被后来任何一次压缩改写。

它**只接自己开的会话**（arm=chat，`configs/chat.yaml`）：实验臂那些会话的 rounds.jsonl
是一等实验数据，不能让聊天窗口往里追加轮次。

刻意做成单文件 + 原生 JS：加一条前端构建链会把这个实验的依赖面撑大，
而它要验证的东西跟打包工具毫无关系。
"""

from __future__ import annotations

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pciso 对话</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e8ee; --dim:#98a0b3;
          --user:#20293a; --accent:#6ea8fe; --bad:#ff6b6b; }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--fg); display:flex; flex-direction:column;
         font:15px/1.7 ui-sans-serif,system-ui,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { flex:0 0 auto; display:flex; align-items:center; gap:10px;
           padding:12px 20px; border-bottom:1px solid var(--line); }
  header .title { font-size:14px; font-weight:650; letter-spacing:.2px; }
  header .who { font-size:12px; color:var(--dim); }
  header .spacer { flex:1; }
  header button { background:none; border:1px solid var(--line); color:var(--dim);
                  border-radius:8px; padding:5px 11px; font:inherit; font-size:12px; cursor:pointer; }
  header button:hover { color:var(--fg); border-color:var(--dim); }
  main { flex:1 1 auto; overflow-y:auto; }
  .stream { max-width:780px; margin:0 auto; padding:26px 20px 34px; }
  .row { display:flex; margin:0 0 20px; }
  .row.user { justify-content:flex-end; }
  .bubble { max-width:82%; padding:11px 15px; border-radius:14px; white-space:pre-wrap;
            word-break:break-word; background:var(--panel); border:1px solid var(--line); }
  .row.user .bubble { background:var(--user); border-color:#2c3648; }
  .bubble.wait { color:var(--dim); }
  .bubble.err { color:var(--bad); border-color:rgba(255,107,107,.45); }
  footer { flex:0 0 auto; border-top:1px solid var(--line); padding:14px 20px 18px; }
  .composer { max-width:780px; margin:0 auto; display:flex; gap:10px; align-items:flex-end; }
  textarea { flex:1; resize:none; min-height:46px; max-height:180px; overflow-y:auto;
             background:var(--panel); color:var(--fg); border:1px solid var(--line);
             border-radius:12px; padding:12px 14px; font:inherit; }
  textarea:focus { outline:none; border-color:var(--accent); }
  .send { background:var(--accent); color:#0b1220; border:none; border-radius:12px;
          padding:12px 18px; font:inherit; font-weight:650; cursor:pointer; }
  .send:disabled { opacity:.45; cursor:not-allowed; }
</style>
</head>
<body>
<header>
  <span class="title">pciso 对话</span>
  <span class="who" id="who"></span>
  <span class="spacer"></span>
  <button id="new">新对话</button>
</header>

<main id="scroll"><div class="stream" id="stream"></div></main>

<footer>
  <div class="composer">
    <textarea id="msg" rows="1" placeholder="说点什么（Enter 发送，Shift+Enter 换行）"></textarea>
    <button class="send" id="send">发送</button>
  </div>
</footer>

<script>
const $ = (id) => document.getElementById(id);
// 对话驱动（不是实验臂）：机制与 C 组一致（每轮只给 P + C + 这一句话），
// 但轮数不封顶、也不按阶段计划催进度 —— 实验臂那 8 轮曲线要留在原地。
const CHAT_CONFIG = 'configs/chat.yaml';
const CHAT_ARM = 'chat';         // 只接管自己开的会话，不给实验会话追加轮次
const PREFIX = 'prefixes/one_sentence_edit';
const KEEP = 'pciso.chat.sid';   // 记住"接着哪段聊"，刷新不另起一段
let sid = null, busy = false;

function esc(t) { return (t || '').replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
function row(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstElementChild; }
function scrollDown() { const m = $('scroll'); m.scrollTop = m.scrollHeight; }

function addUser(text) {
  $('stream').appendChild(row(`<div class="row user"><div class="bubble">${esc(text)}</div></div>`));
  scrollDown();
}

function addAssistant(text, cls) {
  const node = row(`<div class="row"><div class="bubble ${cls || ''}">${esc(text)}</div></div>`);
  $('stream').appendChild(node);
  scrollDown();
  return node.querySelector('.bubble');
}

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function jpost(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(body || {})});
  const text = await r.text();
  if (!r.ok) throw new Error(text);
  return JSON.parse(text);
}

function setWho(label) { $('who').textContent = label; }

// 默认接"自己上一段对话" —— 说出来它就带着事实链，而不是对着一份空状态寒暄。
// **只认 arm=chat 的会话**：实验臂那边的 rounds.jsonl 是一等实验数据。
async function openSession() {
  const kept = localStorage.getItem(KEEP);
  try {
    const list = (await jget('/sessions')).sessions || [];
    const mine = list.filter(s => s.arm === CHAT_ARM);
    const pick = mine.find(s => s.session_id === kept) || mine[0];
    if (pick) {
      sid = pick.session_id;
      localStorage.setItem(KEEP, sid);
      setWho(`${pick.session_id.slice(0,8)} · ${pick.rounds} 轮 · ${pick.arm || '?'}`);
      return;
    }
  } catch (e) { /* 列表拿不到就照常开新的 */ }
  await newSession();
}

async function newSession() {
  const res = await jpost('/session/create', {
    project: 'one_sentence_edit',
    prefix_path: PREFIX,
    config_path: CHAT_CONFIG,
    overrides: { workspace_path: null },   // 对话不写交付仓库
  });
  sid = res.session_id;
  localStorage.setItem(KEEP, sid);
  setWho(`${sid.slice(0,8)} · 0 轮 · ${res.arm || '?'}`);
}

async function restore() {
  if (!sid) return;
  const data = await jget(`/session/${sid}/chat`);
  for (const t of data.turns) {
    addUser(t.message);
    addAssistant(t.reply, t.error ? 'err' : '');
  }
}

async function send() {
  if (busy) return;
  const text = $('msg').value.trim();
  if (!text) return;
  busy = true; $('send').disabled = true;
  $('msg').value = ''; $('msg').style.height = 'auto';
  addUser(text);
  const slot = addAssistant('…（在跑一轮；要动文件的话 1~2 分钟）', 'wait');
  try {
    if (!sid) await newSession();
    const v = await jpost(`/session/${sid}/say`, { message: text });
    slot.textContent = v.reply || '（这一轮没写正文）';
    if (v.reply_kind === 'thinking_fallback') {
      slot.textContent = '（模型这轮没写正文）';
      slot.classList.add('err');
    }
    // 被判回退 = 这一轮写的东西被撤了。窗口不摆过程，但这件事必须说 ——
    // 不说就等于让人以为改动已经落地。
    if (v.verdict && v.verdict !== 'accept') {
      slot.textContent += `\n（这一轮被判 ${v.verdict}：${(v.verdict_reasons || []).join('；').slice(0, 200)}）`;
    }
  } catch (e) {
    slot.textContent = '这一轮没走通：' + e.message.slice(0, 400);
    slot.classList.add('err');
  } finally {
    slot.classList.remove('wait');
    busy = false; $('send').disabled = false; $('msg').focus();
    scrollDown();
  }
}

$('send').onclick = send;
$('new').onclick = async () => {
  localStorage.removeItem(KEEP);
  sid = null;
  $('stream').innerHTML = '';
  try { await newSession(); } catch (e) { addAssistant('开新会话失败：' + e.message.slice(0,200), 'err'); }
  $('msg').focus();
};
$('msg').addEventListener('keydown', (e) => {
  // isComposing：中文输入法敲 Enter 选词时不要当成发送
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$('msg').addEventListener('input', () => {
  const t = $('msg'); t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 180) + 'px';
});

(async () => {
  try {
    const h = await jget('/health');
    if (!h.work_model_ready) setWho(`离线（没配 ${h.api_key_env}）`);
  } catch (e) { /* 顶部这行只是个提示，拿不到就算了 */ }
  try {
    await openSession();
    await restore();
  } catch (e) {
    addAssistant('页面没能接上会话：' + e.message.slice(0, 200), 'err');
  }
})();
</script>
</body>
</html>
"""
