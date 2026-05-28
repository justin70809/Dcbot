const OpenAI = require('openai');
const { Client } = require('@evex/linejs');

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const LINE_EMAIL = process.env.LINE_EMAIL;
const LINE_PASSWORD = process.env.LINE_PASSWORD;
const OPENAI_MODEL = process.env.OPENAI_PRIMARY_MODEL || 'gpt-5.5';
const TRIGGER = process.env.LINEJS_TRIGGER || '!問';

for (const [key, value] of Object.entries({ OPENAI_API_KEY, LINE_EMAIL, LINE_PASSWORD })) {
  if (!value) {
    throw new Error(`缺少必要環境變數：${key}`);
  }
}

const openai = new OpenAI({ apiKey: OPENAI_API_KEY });
const line = new Client({
  auth: {
    email: LINE_EMAIL,
    password: LINE_PASSWORD,
  },
});

function shouldReply(text) {
  if (!text) return false;
  if (text === '!功能' || text === '!help') return true;
  return text.startsWith(`${TRIGGER} `);
}

async function askOpenAI(userText) {
  const prompt = userText.slice(TRIGGER.length).trim();
  const resp = await openai.responses.create({
    model: OPENAI_MODEL,
    input: [
      {
        role: 'system',
        content: '請以繁體中文回覆，先給結論再簡短補充。',
      },
      {
        role: 'user',
        content: prompt,
      },
    ],
  });

  return resp.output_text || '目前沒有可回覆的內容。';
}

async function main() {
  await line.login();
  console.log('LINEJS 已登入，開始監聽訊息。');

  line.on('message', async (msg) => {
    try {
      const text = (msg?.text || '').trim();
      if (!shouldReply(text)) return;

      if (text === '!功能' || text === '!help') {
        await line.replyMessage(msg, [`可用指令：\n1) ${TRIGGER} <問題>\n2) !功能`]);
        return;
      }

      const answer = await askOpenAI(text);
      await line.replyMessage(msg, [answer]);
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
