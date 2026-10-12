/**
 * The conversation stream reader's own contract.
 *
 * This is the boundary between bytes and events: one frame split across reads, two
 * frames in one read, a multi-byte character split across a chunk boundary, a
 * response that is not a stream at all, a frame that cannot be read and a stream
 * that ends before it says how it ended. A reader that dropped any of these would
 * show a client a story the host never told, so each one is pinned here.
 */
import { describe, expect, it } from 'vitest'

import {
  UserStrategyRequestError,
  conversationEvents,
  type UserStrategyConversationEvent,
} from './user-strategies-api'

function streamOf(chunks: (string | Uint8Array)[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
      }
      controller.close()
    },
  })
  return { ok: true, body } as unknown as Response
}

/** The events one reader produced, and the refusal that ended it if there was one. */
async function read(response: Response): Promise<{
  events: UserStrategyConversationEvent[]
  failure: unknown
}> {
  const events: UserStrategyConversationEvent[] = []
  try {
    for await (const event of conversationEvents(response)) events.push(event)
    return { events, failure: null }
  } catch (error) {
    return { events, failure: error }
  }
}

const delta = (text: string) => `data: ${JSON.stringify({ kind: 'text_delta', text })}\n\n`
const done = 'data: {"kind":"done","strategy":{"user_strategy_id":"us-1"}}\n\n'

describe('conversation stream reader', () => {
  it('passes each event as soon as its frame is complete, in order', async () => {
    const { events, failure } = await read(streamOf([
      delta('第一'),
      delta('段'),
      done,
    ]))

    expect(failure).toBeNull()
    expect(events.map(event => event.kind)).toEqual(['text_delta', 'text_delta', 'done'])
    expect(events.slice(0, 2).map(event => (event as { text: string }).text)).toEqual(['第一', '段'])
  })

  it('reads a frame split across reads and a character split across bytes', async () => {
    const bytes = new TextEncoder().encode(delta('中文回答') + done)
    // The cut lands inside a multi-byte character, and the frame is not even half
    // delivered: both have to be reassembled before anything is emitted.
    const cut = bytes.indexOf(0xe4) + 1
    const { events, failure } = await read(streamOf([bytes.slice(0, cut), bytes.slice(cut, cut + 5), bytes.slice(cut + 5)]))

    expect(failure).toBeNull()
    expect((events[0] as { text: string }).text).toBe('中文回答')
    expect(events[1].kind).toBe('done')
  })

  it('reads several frames that arrive together, and a trailing frame with no blank line', async () => {
    const { events, failure } = await read(streamOf([
      delta('a') + delta('b'),
      // The last frame arrives without its terminating blank line; the end of the
      // body is the end of the frame.
      'data: {"kind":"interrupt","waiting":{"kind":"proposal","proposal_id":"prop-1"},"strategy":{"user_strategy_id":"us-1"}}',
    ]))

    expect(failure).toBeNull()
    expect(events.map(event => event.kind)).toEqual(['text_delta', 'text_delta', 'interrupt'])
  })

  it('refuses a response that is not the stream, with the host\'s own code', async () => {
    const response = {
      ok: false,
      json: async () => ({ error: { code: 'user_strategy_invalid', message: '这条策略不等待这个决定。' } }),
    } as unknown as Response

    const { events, failure } = await read(response)

    expect(events).toEqual([])
    expect(failure).toBeInstanceOf(UserStrategyRequestError)
    expect((failure as UserStrategyRequestError).code).toBe('user_strategy_invalid')
    expect(String((failure as UserStrategyRequestError).message)).toContain('不等待')
  })

  it('refuses a frame it cannot read, after the events that really arrived', async () => {
    const { events, failure } = await read(streamOf([
      delta('已经到达的增量'),
      'data: {"kind": not json\n\n',
      done,
    ]))

    // What arrived is shown; what could not be read is refused rather than skipped.
    expect(events.map(event => event.kind)).toEqual(['text_delta'])
    expect((failure as UserStrategyRequestError).code).toBe('stream_unreadable')
  })

  it('refuses a stream that ends before it says how it ended', async () => {
    const { events, failure } = await read(streamOf([delta('半句话'), delta('没有结论')]))

    expect(events).toHaveLength(2)
    expect((failure as UserStrategyRequestError).code).toBe('stream_incomplete')
  })
  it('preserves a structured failure and releases a cancelled reader', async () => {
    const response = streamOf(['data: {"kind":"error","code":"store_unavailable","error":"数据库不可读"}\n\n'])
    const { failure } = await read(response)
    expect((failure as UserStrategyRequestError).code).toBe('store_unavailable')
    expect((failure as Error).message).toBe('数据库不可读')
    expect(response.body?.locked).toBe(false)
  })

})
