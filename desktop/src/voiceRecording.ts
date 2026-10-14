export function voiceInputFailure(cause: unknown): string {
  const name = cause instanceof Error ? cause.name : String(cause || "");
  const reasons: Record<string, string> = {
    NotAllowedError: "墨流没有获得麦克风使用许可。请检查系统的麦克风权限，再点语音重试。",
    NotFoundError: "没有找到可用的麦克风。请接好设备，或在语音设置里选择当前输入设备。",
    OverconstrainedError: "原来选择的麦克风已经不可用。请重新选择输入设备。",
    NotReadableError: "麦克风无法开始录音，可能被其他程序占用或设备连接异常。请关闭占用录音的程序后重试。",
    "not-allowed": "浏览器没有获得麦克风使用许可。请检查麦克风权限。",
    "audio-capture": "浏览器没有收到麦克风声音。请检查输入设备。",
    network: "浏览器在线听写服务连接失败。可在语音设置中改用已安装的本地识别。",
    "service-not-allowed": "当前浏览器环境无法使用在线听写服务。可在语音设置中改用本地识别。",
    "no-speech": "暂时没有听到清晰说话声。请靠近麦克风，确认没有静音后再试。",
  };
  return reasons[name] || (cause instanceof Error ? cause.message : "语音输入没有完成，请查看麦克风和识别组件状态后重试。");
}

export class LocalWavRecorder {
  private chunks: Float32Array[] = [];
  private length = 0;
  private peak = 0;
  private stopped: Promise<Uint8Array> | null = null;
  deviceFallback = false;

  get seconds(): number { return this.length / this.context.sampleRate; }
  get hasSignal(): boolean { return this.peak > .001; }

  private constructor(
    private readonly stream: MediaStream,
    private readonly context: AudioContext,
    private readonly source: MediaStreamAudioSourceNode,
    private readonly processor: ScriptProcessorNode,
    private readonly silentGain: GainNode,
  ) {}

  static async start(deviceId = "", maxSeconds = 0): Promise<LocalWavRecorder> {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前窗口无法访问麦克风，请重新打开墨流后再试。");
    const capture = async (audio: boolean | MediaTrackConstraints): Promise<MediaStream> => {
      let expired = false, timer: ReturnType<typeof setTimeout> | undefined;
      const request = navigator.mediaDevices.getUserMedia({ audio });
      void request.then(stream => { if (expired) stream.getTracks().forEach(track => track.stop()); }, () => undefined);
      try {
        return await Promise.race([request, new Promise<never>((_, reject) => {
          timer = setTimeout(() => { expired = true; reject(new Error("麦克风没有及时响应。请查看是否有等待你确认的麦克风提示，或设备是否被其他程序占用，再点语音重试。")); }, 30000);
        })]);
      } finally { if (timer !== undefined) clearTimeout(timer); }
    };
    let stream: MediaStream, fallback = false;
    try { stream = await capture(deviceId ? { deviceId: { exact: deviceId } } : true); }
    catch (cause) {
      if (!deviceId || !["NotFoundError", "OverconstrainedError"].includes((cause as Error)?.name)) throw cause;
      stream = await capture(true); fallback = true;
    }
    let context: AudioContext | undefined;
    try {
      context = new AudioContext();
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([context.resume(), new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("麦克风已连接，但录音没有启动。请再点一次语音；如果仍无声音，检查当前输入设备。")), 10000);
        })]);
      } finally { if (timer !== undefined) clearTimeout(timer); }
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(4096, 1, 1);
      const silentGain = context.createGain();
      silentGain.gain.value = 0;
      const recorder = new LocalWavRecorder(stream, context, source, processor, silentGain);
      recorder.deviceFallback = fallback;
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const remaining = maxSeconds > 0 ? Math.max(0, Math.floor(recorder.context.sampleRate * maxSeconds) - recorder.length) : input.length;
        const copy = new Float32Array(input.subarray(0, remaining));
        if (!copy.length) return;
        recorder.chunks.push(copy);
        recorder.length += copy.length;
        for (const sample of copy) recorder.peak = Math.max(recorder.peak, Math.abs(sample));
      };
      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(context.destination);
      return recorder;
    } catch (cause) {
      stream.getTracks().forEach(track => track.stop());
      if (context && context.state !== "closed") await context.close().catch(() => undefined);
      throw cause;
    }
  }

  stop(): Promise<Uint8Array> {
    return this.stopped ??= this.finish();
  }

  private async finish(): Promise<Uint8Array> {
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
