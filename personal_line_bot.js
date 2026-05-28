import OpenAI from 'openai';
import { Pool } from 'pg';
import { loginWithPassword } from '@evex/linejs';

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const LINE_EMAIL = process.env.LINE_EMAIL;
const LINE_PASSWORD = process.env.LINE_PASSWORD;
const DATABASE_URL = process.env.DATABASE_URL;
const OPENAI_PRIMARY_MODEL = process.env.OPENAI_PRIMARY_MODEL || 'gpt-5.5';
const OPENAI_SUMMARY_MODEL = process.env.OPENAI_SUMMARY_MODEL || 'gpt-5.4-mini';
const TRIGGER = process.env.LINEJS_TRIGGER || '!問';
const REPLY_DELAY_MIN_MS = Number.parseInt(process.env.LINEJS_REPLY_DELAY_MIN_MS || '1200', 10);
const REPLY_DELAY_MAX_MS = Number.parseInt(process.env.LINEJS_REPLY_DELAY_MAX_MS || '4500', 10);
const MAX_IMAGES_PER_REQUEST = Number.parseInt(process.env.LINEJS_MAX_IMAGES_PER_REQUEST || '10', 10);
const IMAGE_BUFFER_TTL_MS = Number.parseInt(process.env.LINEJS_IMAGE_BUFFER_TTL_MS || '600000', 10);
const MAX_IMAGE_BYTES = Number.parseInt(process.env.LINEJS_MAX_IMAGE_BYTES || '8388608', 10);
const OPENAI_ENABLE_WEB_SEARCH = ['1', 'true', 'yes', 'on'].includes((process.env.OPENAI_ENABLE_WEB_SEARCH || 'true').toLowerCase());
const FEATURE_LIST_COMMANDS = new Set(['!功能', '!功能列表', '!help']);
const roomImageBuffers = new Map();

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
  const toType = msg?.to?.type || '';
  return ['GROUP', 'ROOM'].includes(String(toType).toUpperCase());
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomInt(min, max) {
  const lower = Math.ceil(Math.min(min, max));
  const upper = Math.floor(Math.max(min, max));
  return Math.floor(Math.random() * (upper - lower + 1)) + lower;
}

async function randomReplyDelay() {
  const delay = randomInt(REPLY_DELAY_MIN_MS, REPLY_DELAY_MAX_MS);
  await sleep(delay);
}

async function safeReadMessage(msg) {
  try {
    if (typeof msg?.read === 'function') {
      await msg.read();
    }
  } catch (err) {
    console.warn('訊息已讀失敗：', err);
  }
}

function isAskCommand(text) {
  return text.startsWith(`${TRIGGER} `);
}

function stripTrigger(text) {
  return isAskCommand(text) ? text.slice(TRIGGER.length).trim() : text;
}

function getChatRoomId(msg) {
  return String(
    msg?.to?.mid
    || msg?.to?.id
    || msg?.chatMid
    || msg?.chatId
    || msg?.squareChatMid
    || msg?.squareChatId
    || msg?.to?.squareChatMid
    || 'unknown-room',
  );
}

function isImageMessage(msg) {
  const rawType = String(msg?.contentType || msg?.type || msg?.messageType || '').toUpperCase();
  return rawType.includes('IMAGE') || rawType === '1' || rawType === 'PHOTO';
}

function cleanupExpiredImages(roomId) {
  const bucket = roomImageBuffers.get(roomId);
  if (!bucket || bucket.length === 0) return;
  const now = Date.now();
  const valid = bucket.filter((item) => now - item.createdAt <= IMAGE_BUFFER_TTL_MS);
  if (valid.length > 0) {
    roomImageBuffers.set(roomId, valid);
  } else {
    roomImageBuffers.delete(roomId);
  }
}

function popBufferedImages(roomId) {
  cleanupExpiredImages(roomId);
  const bucket = roomImageBuffers.get(roomId) || [];
  roomImageBuffers.delete(roomId);
  return bucket.slice(-MAX_IMAGES_PER_REQUEST).map((item) => item.dataUrl);
}

async function bufferIncomingImage(msg, roomId) {
  if (typeof msg?.getData !== 'function') return;
  const imageData = await msg.getData();
  if (!imageData) return;
  const byteLength = Buffer.isBuffer(imageData)
    ? imageData.byteLength
    : Buffer.byteLength(imageData);
  if (byteLength > MAX_IMAGE_BYTES) {
    console.warn(`圖片略過：超過大小上限 ${byteLength} bytes > ${MAX_IMAGE_BYTES} bytes`);
    return;
  }
  const buffer = Buffer.isBuffer(imageData) ? imageData : Buffer.from(imageData);
  const dataUrl = `data:image/jpeg;base64,${buffer.toString('base64')}`;
  cleanupExpiredImages(roomId);
  const existing = roomImageBuffers.get(roomId) || [];
  existing.push({ dataUrl, createdAt: Date.now() });
  roomImageBuffers.set(roomId, existing.slice(-MAX_IMAGES_PER_REQUEST));
}

async function replyLineMessage(msg, texts) {
  for (const text of texts) {
    await randomReplyDelay();
    await msg.reply(String(text));
  }
}

function shouldReply(text, groupChat) {
  if (!text) return false;
  if (text === '!清空記憶') return true;
  if (FEATURE_LIST_COMMANDS.has(text)) return true;
  if (groupChat) return text.startsWith(`${TRIGGER} `);
  return true;
}

async function askOpenAI(userText, userId, imageDataUrls = []) {
  const prompt = stripTrigger(userText);
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
    input: [{
      role: 'user',
      content: [
        { type: 'input_text', text: buildAskUserText(prompt, state.summary, isFirstTurn) },
        ...imageDataUrls.map((imageUrl) => ({ type: 'input_image', image_url: imageUrl })),
      ],
    }],
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

  const line = await loginWithPassword(
    {
      email: LINE_EMAIL,
      password: LINE_PASSWORD,
      onPincodeRequest(pin) {
        console.log('LINE 登入 PIN:', pin);
      },
    },
    {
      device: 'DESKTOPWIN',
    },
  );

  console.log('LINEJS 已登入，開始監聽訊息。');

  const handleLineMessage = async (msg) => {
    try {
      const myMessage = typeof msg.isMyMessage === 'function' ? await msg.isMyMessage() : msg.isMyMessage;
      if (myMessage) return;

      await safeReadMessage(msg);
      const roomId = getChatRoomId(msg);
      if (isImageMessage(msg)) {
        await bufferIncomingImage(msg, roomId);
        return;
      }
      const text = (msg?.text || '').trim();
      const groupChat = msg.isSquare || isGroupChat(msg);
      if (!text) return;
      if (groupChat && text !== '!清空記憶' && text !== '!功能' && !isAskCommand(text)) return;
      if (!groupChat && !shouldReply(text, groupChat)) return;

      const userId = `line-${msg?.from?.mid || msg?.from?.id || msg?.sender || 'unknown'}`;

      if (text === '!清空記憶') {
        await clearUserMemory(userId);
        roomImageBuffers.delete(roomId);
        await replyLineMessage(msg, ['已為你完全清空記憶（摘要、對話串接、計數）。']);
        return;
      }

      if (!groupChat && FEATURE_LIST_COMMANDS.has(text)) {
        await replyLineMessage(msg, [
          '可用功能：',
          `1) ${TRIGGER} <問題>：向 AI 詢問問題（群組需加關鍵字）`,
          '2) !清空記憶：清除你的對話記憶',
          '3) !功能 或 !功能列表：查看這份功能清單',
        ]);
        return;
      }

      if (!groupChat && !isAskCommand(text)) {
        await replyLineMessage(msg, [
          `請使用：${TRIGGER} <你的問題>`,
          `例如：${TRIGGER} 幫我整理今天 AI 重點新聞`,
          '若要清空記憶：!清空記憶',
        ]);
        return;
      }

      const imageDataUrls = isAskCommand(text) ? popBufferedImages(roomId) : [];
      const answers = await askOpenAI(text, userId, imageDataUrls);
      await replyLineMessage(msg, answers.slice(0, 5));
    } catch (err) {
      console.error('訊息處理失敗：', err);
      try {
        await randomReplyDelay();
        await replyLineMessage(msg, ['抱歉，剛剛處理失敗，請稍後再試。']);
      } catch (_) {}
    }
  };

  line.on('message', handleLineMessage);
  line.on('square:message', handleLineMessage);

  line.listen({ talk: true, square: true });
}

main().catch((err) => {
  console.error('啟動失敗：', err);
  process.exit(1);
});
