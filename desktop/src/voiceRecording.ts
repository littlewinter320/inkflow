export class LocalWavRecorder {
  private chunks: Float32Array[] = [];
  private length = 0;

  private constructor(
    private readonly stream: MediaStream,
    private readonly context: AudioContext,
    private readonly source: MediaStreamAudioSourceNode,
    private readonly processor: ScriptProcessorNode,
    private readonly silentGain: GainNode,
  ) {}

  static async start(deviceId = ""): Promise<LocalWavRecorder> {
    const audio = deviceId ? { deviceId: { exact: deviceId } } : true;
    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const silentGain = context.createGain();
    silentGain.gain.value = 0;
    const recorder = new LocalWavRecorder(stream, context, source, processor, silentGain);
    processor.onaudioprocess = (event) => {
      const copy = new Float32Array(event.inputBuffer.getChannelData(0));
      recorder.chunks.push(copy);
      recorder.length += copy.length;
    };
    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(context.destination);
    return recorder;
  }

  async stop(): Promise<Uint8Array> {
    const sampleRate = this.context.sampleRate;
    this.processor.onaudioprocess = null;
    this.source.disconnect();
    this.processor.disconnect();
    this.silentGain.disconnect();
    this.stream.getTracks().forEach((track) => track.stop());
    await this.context.close();
    const samples = new Float32Array(this.length);
    let offset = 0;
    for (const chunk of this.chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    return encodeMonoWav(samples, sampleRate);
  }

  // 边听边出字：把当前已积累的音频编码成 WAV 快照，不停止录音。
  // JS 单线程，编码期间 onaudioprocess 不会并发写入 chunks，安全。
  snapshot(): Uint8Array {
    const samples = new Float32Array(this.length);
    let offset = 0;
    for (const chunk of this.chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    return encodeMonoWav(samples, this.context.sampleRate);
  }
}

function encodeMonoWav(samples: Float32Array, sampleRate: number): Uint8Array {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return new Uint8Array(bytes);
}

function writeAscii(view: DataView, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index));
}
