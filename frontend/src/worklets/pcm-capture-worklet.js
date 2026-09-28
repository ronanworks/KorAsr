// AudioWorklet runs off the UI thread. Send exact 4096-sample float32 blocks
// from the 16 kHz AudioContext to the browser WebSocket.
const BLOCK_SAMPLES = 4096

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.block = new Float32Array(BLOCK_SAMPLES)
    this.offset = 0
  }

  process(inputs, outputs) {
    const samples = inputs[0]?.[0]
    if (samples) {
      let index = 0
      while (index < samples.length) {
        const count = Math.min(samples.length - index, BLOCK_SAMPLES - this.offset)
        this.block.set(samples.subarray(index, index + count), this.offset)
        this.offset += count
        index += count
        if (this.offset === BLOCK_SAMPLES) {
          this.port.postMessage(this.block.buffer, [this.block.buffer])
          this.block = new Float32Array(BLOCK_SAMPLES)
          this.offset = 0
        }
      }
    }
    // Keep the processing graph alive without playing the microphone aloud.
    outputs[0]?.[0]?.fill(0)
    return true
  }
}

registerProcessor('korasr-pcm-capture', PcmCaptureProcessor)
