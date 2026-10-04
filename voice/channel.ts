#!/usr/bin/env bun
// Канал voxcode: голосовые команды со слушателя (listener.py) -> сессия помощника,
// ответы помощника (инструмент say) -> слушатель, который их озвучивает.
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { existsSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { randomBytes } from 'crypto'

const PORT = 8790
// Общий секрет со слушателем. Нестандартный заголовок блокирует запросы
// с веб-страниц (CORS preflight), так что браузер команду не подсунет.
const TOKEN_FILE = join(import.meta.dir, '.token')
if (!existsSync(TOKEN_FILE)) writeFileSync(TOKEN_FILE, randomBytes(24).toString('hex'))
const TOKEN = readFileSync(TOKEN_FILE, 'utf8').trim()

const listeners = new Set<(chunk: string) => void>()
function broadcast(event: object) {
  const chunk = `data: ${JSON.stringify(event)}\n\n`
  for (const emit of listeners) emit(chunk)
}

const mcp = new Server(
  { name: 'voxcode', version: '0.1.0' },
  {
    capabilities: { experimental: { 'claude/channel': {} }, tools: {} },
    instructions:
      'Команды владельца с ПК приходят как <channel source="voxcode" chat_id="..." via="...">текст</channel>. ' +
      'via="voice" — распознанная речь с микрофона (возможны ошибки распознавания, при неоднозначности переспроси); ' +
      'via="text" — набрано в панели помощника; via="button" — кнопка быстрого действия (/brief, /tasks, /mail, /cal, /projects — как в Telegram). ' +
      'Ответ показывается текстом в панели и озвучивается. Для сводок и списков (особенно via="button") ' +
      'кратко скажи суть в text, а сам список/подробности положи в details — они видны в панели, но не звучат. ' +
      'Отвечай ТОЛЬКО через инструмент say (он озвучит ответ в колонках владельца): 1–3 коротких разговорных предложения, ' +
      'без markdown, списков, кода, путей и ссылок. Подробности (код, списки) пиши в файл и скажи, куда положил. ' +
      'Короткое «угу / секунду» панель говорит сама сразу после фразы владельца — не повторяй его. ' +
      'Если для ответа нужны инструменты (почта, файлы, команды, поиск) — сначала короткий say о том, что именно делаешь ' +
      '(«Гляну почту», «Запускаю проверку лимитов»), потом say с результатом. Ответ без инструментов — сразу результат. ' +
      'На каждое сообщение отсюда обязательно ответь через say, хотя бы одним словом: иначе панель висит в «думаю». ' +
      'Приказ («сделай», «выполни», «делай», «продолжай», «проверь») — выполняй сразу. Вопрос или обсуждение ' +
      '(«как лучше», «что думаешь», «хотелось бы», вопрос в конце фразы) — ничего не меняй: ответь, предложи 2–3 варианта ' +
      'со своей рекомендацией и жди решения владельца; приказ с вопросом в конце — сначала ответ с вариантами. ' +
      'Варианты передавай в say полем options (2–4 коротких подписи, рекомендуемый первым и с «⭐»): в панели это кнопки. ' +
      'via="choice" — владелец нажал кнопку варианта: выполняй выбранное. Голосом он может сказать «первый», «второй» и т. п. — это тоже выбор. ' +
      'Режим диктовки: via="dictation" — фраза после паузы. Ответ на твой вопрос («да», «готово», «давай») ' +
      'или законченная команда — действуй по правилу выше сразу, «выполняй» не жди. ' +
      'Если фраза явно оборвана или это кусок длинного задания — ничего не делай, ответь одним-двумя словами ' +
      '(«Понял», «Записал про кнопку»). Никогда не спрашивай «что ещё?» / «есть ли ещё что-то?». ' +
      'via="dictation_end" — владелец сказал «всё» / «выполняй»: выполни всё надиктованное, что ещё не сделано, ' +
      'как одну задачу. Правила подтверждения опасных действий действуют всегда: подтверждение спроси через say и жди «да».',
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'say',
    description: 'Озвучить ответ владельцу через колонки ПК (голосовой канал voxcode).',
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'Короткий разговорный ответ на русском, без markdown — его озвучат' },
        details: { type: 'string', description: 'Необязательно: подробности (списки, сводка, цифры) — показываются в панели текстом, НЕ озвучиваются. Простой текст, переносы строк, можно «•» для пунктов.' },
        options: { type: 'array', items: { type: 'string' }, description: 'Необязательно: 2–4 варианта на выбор (короткие подписи) — в панели кнопки, нажатие придёт как via="choice". Рекомендуемый — первым, с «⭐».' },
        chat_id: { type: 'string', description: 'chat_id из входящего <channel>' },
      },
      required: ['text'],
    },
  }],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name === 'say') {
    const { text, details, options, chat_id } = req.params.arguments as { text: string; details?: string; options?: string[]; chat_id?: string }
    if (listeners.size === 0) return { content: [{ type: 'text', text: 'слушатель не подключён — ответ не озвучен' }] }
    broadcast({ type: 'say', text, details, options, chat_id })
    return { content: [{ type: 'text', text: 'озвучено' }] }
  }
  throw new Error(`unknown tool: ${req.params.name}`)
})

await mcp.connect(new StdioServerTransport())
// Сессия Claude закрылась -> выходим, иначе HTTP-сервер держит порт и процесс висит
mcp.onclose = () => process.exit(0)
process.stdin.on('end', () => process.exit(0))
process.stdin.on('close', () => process.exit(0))

let nextId = 1
Bun.serve({
  port: PORT,
  hostname: '127.0.0.1',
  idleTimeout: 0,
  async fetch(req) {
    if (req.headers.get('X-VoxCode-Token') !== TOKEN) return new Response('forbidden', { status: 403 })
    const url = new URL(req.url)

    if (req.method === 'GET' && url.pathname === '/events') {
      const stream = new ReadableStream({
        start(ctrl) {
          ctrl.enqueue(': connected\n\n')
          const emit = (chunk: string) => ctrl.enqueue(chunk)
          listeners.add(emit)
          req.signal.addEventListener('abort', () => listeners.delete(emit))
        },
      })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache' } })
    }

    if (req.method === 'GET' && url.pathname === '/ping') return new Response('pong')

    if (req.method === 'POST' && url.pathname === '/say') {
      const text = (await req.text()).trim()
      if (!text) return new Response('empty', { status: 400 })
      // voice — сказано в микрофон, text — набрано в панели, button — кнопка быстрого действия,
      // dictation — кусок диктовки, dictation_end — конец диктовки, choice — кнопка варианта из say.options
      const via = { voice: 'voice', text: 'text', button: 'button', dictation: 'dictation', dictation_end: 'dictation_end', choice: 'choice' }[
        req.headers.get('X-VoxCode-Source') ?? ''] ?? 'voice'
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: { content: text, meta: { chat_id: String(nextId++), via } },
      })
      return new Response('ok')
    }
    return new Response('not found', { status: 404 })
  },
})
