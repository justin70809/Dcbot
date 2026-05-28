const OpenAI = require('openai');
const { Pool } = require('pg');
const { Client } = require('@evex/linejs');

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const LINE_EMAIL = process.env.LINE_EMAIL;
const LINE_PASSWORD = process.env.LINE_PASSWORD;
const DATABASE_URL = process.env.DATABASE_URL;
const OPENAI_PRIMARY_MODEL = process.env.OPENAI_PRIMARY_MODEL || 'gpt-5.5';
const OPENAI_SUMMARY_MODEL = process.env.OPENAI_SUMMARY_MODEL || 'gpt-5.4-mini';
const TRIGGER = process.env.LINEJS_TRIGGER || '!問';
const OPENAI_ENABLE_WEB_SEARCH = ['1', 'true', 'yes', 'on'].includes((process.env.OPENAI_ENABLE_WEB_SEARCH || 'true').toLowerCase());
const FEATURE_LIST_COMMANDS = new Set(['!功能', '!功能列表', '!help']);

for (const [key, value] of Object.entries({ OPENAI_API_KEY, LINE_EMAIL, LINE_PASSWORD, DATABASE_URL })) {
  if (!value) {
    throw new Error(`缺少必要環境變數：${key}`);
  }
}

const ASK_INSTRUCTIONS = [
  '使用繁體中文。',
  '- 回答要精簡，優先用 3-6 行完成重點。',
  '- 先給直接答案，再補充理由與步驟。',
  '- 若資訊不確定要明確說不確定，不能編造。',
  '- 涉及「最新/今天/即時」資訊時，優先使用網路搜尋工具查證後再回答。',
  '- 不要回覆你「無法連網」；若搜尋工具失敗，請明確說是工具暫時失敗並提供可行替代方案。',
].join('\n');

const openai = new OpenAI({ apiKey: OPENAI_API_KEY });
const db = new Pool({ connectionString: DATABASE_URL, ssl: { rejectUnauthorized: false } });
const line = new Client({
  auth: {
    email: LINE_EMAIL,
    password: LINE_PASSWORD,
  },
});

async function initDb() {
  await db.query(`
    CREATE TABLE IF NOT EXISTS memory (
      user_id TEXT PRIMARY KEY,
      summary TEXT,
      token_accum INTEGER,
      last_response_id TEXT,
      thread_count INTEGER
    )
  `);
}

async function loadUserMemory(userId) {
  const { rows } = await db.query(
    `SELECT summary, token_accum, last_response_id, thread_count FROM memory WHERE user_id = $1`,
    [userId],
  );

  if (rows.length > 0) {
    const row = rows[0];
    return {
      summary: row.summary || '',
      token_accum: row.token_accum || 0,
      last_response_id: row.last_response_id || null,
      thread_count: row.thread_count || 0,
    };
  }

  return { summary: '', token_accum: 0, last_response_id: null, thread_count: 0 };
}

async function saveUserMemory(userId, state) {
  await db.query(
    `
    INSERT INTO memory (user_id, summary, token_accum, last_response_id, thread_count)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id) DO UPDATE SET
      summary = EXCLUDED.summary,
      token_accum = EXCLUDED.token_accum,
      last_response_id = EXCLUDED.last_response_id,
      thread_count = EXCLUDED.thread_count
    `,
    [userId, state.summary, state.token_accum, state.last_response_id, state.thread_count],
  );
}

async function clearUserMemory(userId) {
  await db.query('DELETE FROM memory WHERE user_id = $1', [userId]);
}

function splitTextForLine(text, chunkSize = 4900) {
  if (!text) return ['（無內容）'];
  const chunks = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= chunkSize) {
      chunks.push(remaining);
      break;
    }
    let cut = remaining.lastIndexOf('\n', chunkSize);
    if (cut === -1) cut = chunkSize;
    chunks.push(remaining.slice(0, cut).trim());
    remaining = remaining.slice(cut).trim();
  }

  return chunks;
}

function buildAskUserText(prompt, summary, isFirstTurn) {
  const now = new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Taipei', hour12: false }).replace('T', ' ');
  return [
    '<context>',
    'timezone=Asia/Taipei',
    `current_time=${now}`,
    `first_turn=${isFirstTurn ? 'yes' : 'no'}`,
    `memory_summary=${summary || '（無）'}`,
    '</context>',
    '',
    '<user_query>',
    prompt,
    '</user_query>',
  ].join('\n');
}

function isGroupChat(msg) {
  const sourceType = msg?.chatType || msg?.toType || msg?.roomType || msg?.sourceType || '';
  return ['group', 'room'].includes(String(sourceType).toLowerCase());
}

function shouldReply(text, groupChat) {
  if (!text) return false;
  if (text === '!清空記憶') return true;
  if (FEATURE_LIST_COMMANDS.has(text)) return true;
  if (groupChat) return text.startsWith(`${TRIGGER} `);
  return true;
}

async function askOpenAI(userText, userId) {
  const prompt = userText.startsWith(`${TRIGGER} `) ? userText.slice(TRIGGER.length).trim() : userText;
  const state = await loadUserMemory(userId);
  state.thread_count = (state.thread_count || 0) + 1;

  const isFirstTurn = state.thread_count === 1 && !state.last_response_id;

  if (state.thread_count >= 10 && state.last_response_id) {
    const summaryResp = await openai.responses.create({
      model: OPENAI_SUMMARY_MODEL,
      previous_response_id: state.last_response_id,
      input: [{ role: 'user', content: '請將本輪對話濃縮成 100 字以內記憶摘要，保留使用者偏好與重要背景。' }],
      store: false,
    });
    state.summary = summaryResp.output_text || state.summary;
    state.last_response_id = null;
    state.thread_count = 0;
  }

  const request = {
    model: OPENAI_PRIMARY_MODEL,
    instructions: ASK_INSTRUCTIONS,
    input: [{ role: 'user', content: [{ type: 'input_text', text: buildAskUserText(prompt, state.summary, isFirstTurn) }] }],
    previous_response_id: state.last_response_id,
    store: true,
  };

  if (OPENAI_ENABLE_WEB_SEARCH) {
    request.tools = [{ type: 'web_search', external_web_access: true }];
    request.tool_choice = 'auto';
  }

  const resp = await openai.responses.create(request);
  state.last_response_id = resp.id;
  await saveUserMemory(userId, state);

  return splitTextForLine(resp.output_text);
}

async function main() {
  await initDb();
  await line.login();
  console.log('LINEJS 已登入，開始監聽訊息。');

  line.on('message', async (msg) => {
    try {
      const text = (msg?.text || '').trim();
      const groupChat = isGroupChat(msg);
      if (!shouldReply(text, groupChat)) return;

      const userId = `line-${msg?.from?.mid || msg?.from?.id || msg?.sender || 'unknown'}`;

      if (text === '!清空記憶') {
        await clearUserMemory(userId);
        await line.replyMessage(msg, ['已為你完全清空記憶（摘要、對話串接、計數）。']);
        return;
      }

      if (FEATURE_LIST_COMMANDS.has(text)) {
        await line.replyMessage(msg, [
          '可用功能：',
          `1) ${TRIGGER} <問題>：向 AI 詢問問題（群組需加關鍵字）`,
          '2) !清空記憶：清除你的對話記憶',
          '3) !功能 或 !功能列表：查看這份功能清單',
        ]);
        return;
      }

      if (!groupChat && !text.startsWith(`${TRIGGER} `)) {
        await line.replyMessage(msg, [
          `請使用：${TRIGGER} <你的問題>`,
          `例如：${TRIGGER} 幫我整理今天 AI 重點新聞`,
          '若要清空記憶：!清空記憶',
        ]);
        return;
      }

      const answers = await askOpenAI(text, userId);
      await line.replyMessage(msg, answers.slice(0, 5));
    } catch (err) {
      console.error('訊息處理失敗：', err);
      try {
        await line.replyMessage(msg, ['抱歉，剛剛處理失敗，請稍後再試。']);
      } catch (_) {}
    }
  });
}

main().catch((err) => {
  console.error('啟動失敗：', err);
  process.exit(1);
});
