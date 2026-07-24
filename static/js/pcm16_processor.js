class Pcm16Processor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.ratio = sampleRate / 16000;
        this.samples = [];
        this.position = 0;
        this.pending = [];
        this.port.onmessage = (event) => {
            if (event.data === 'flush') this.flush();
        };
    }

    process(inputs) {
        const channel = inputs[0]?.[0];
        if (!channel) return true;

        for (let i = 0; i < channel.length; i += 1) this.samples.push(channel[i]);

        while (this.position + 1 < this.samples.length) {
            const index = Math.floor(this.position);
            const fraction = this.position - index;
            const interpolated = this.samples[index] * (1 - fraction)
                + this.samples[index + 1] * fraction;
            const value = Math.max(-1, Math.min(1, interpolated));
            this.pending.push(value < 0 ? value * 0x8000 : value * 0x7fff);
            this.position += this.ratio;
        }

        const consumed = Math.floor(this.position);
        if (consumed) {
            this.samples.splice(0, consumed);
            this.position -= consumed;
        }

        while (this.pending.length >= 4096) this.emit(4096);
        return true;
    }

    emit(length) {
        const values = this.pending.splice(0, length);
        const pcm = new Int16Array(values.length);
        for (let i = 0; i < values.length; i += 1) pcm[i] = values[i];
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }

    flush() {
        if (this.pending.length) this.emit(this.pending.length);
    }
}

registerProcessor('pcm16-processor', Pcm16Processor);
