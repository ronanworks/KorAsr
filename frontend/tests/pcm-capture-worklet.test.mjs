import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'

test('worklet emits exact 4096-sample PCM blocks across render frames', () => {
  const blocks = []
  let Processor
  class FakeAudioWorkletProcessor {
    constructor() {
      this.port = { postMessage: buffer => blocks.push(new Float32Array(buffer).slice()) }
    }
  }
  const source = readFileSync(new URL('../src/worklets/pcm-capture-worklet.js', import.meta.url), 'utf8')
  runInNewContext(source, {
    AudioWorkletProcessor: FakeAudioWorkletProcessor,
    Float32Array,
    Math,
    registerProcessor: (_name, implementation) => { Processor = implementation },
  })

  const processor = new Processor()
  for (let frame = 0; frame < 33; frame++) {
    const input = Float32Array.from({ length: 128 }, (_, index) => frame * 128 + index)
    const output = new Float32Array(128).fill(1)
    assert.equal(processor.process([[input]], [[output]]), true)
    assert.equal(output.every(value => value === 0), true)
  }
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].length, 4096)
  assert.equal(blocks[0][0], 0)
  assert.equal(blocks[0][4095], 4095)
})
