import { Pause, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";

interface WaveformPlayerProps {
  audioPath: string;
  label: string;
}

export function WaveformPlayer({ audioPath, label }: WaveformPlayerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const waveRef = useRef<WaveSurfer | null>(null);
  const [ready, setReady] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    setReady(false);
    setIsPlaying(false);
    setError(null);
    const wave = WaveSurfer.create({
      container: containerRef.current,
      url: `/api/audio?path=${encodeURIComponent(audioPath)}`,
      waveColor: "#d4d4d4",
      progressColor: "#111111",
      cursorColor: "#0070f3",
      height: 36,
      barWidth: 2,
      barGap: 2,
      barRadius: 2,
      normalize: true
    });
    waveRef.current = wave;
    wave.on("ready", () => setReady(true));
    wave.on("play", () => setIsPlaying(true));
    wave.on("pause", () => setIsPlaying(false));
    wave.on("finish", () => setIsPlaying(false));
    wave.on("error", (nextError) => setError(String(nextError)));
    return () => {
      wave.destroy();
      waveRef.current = null;
    };
  }, [audioPath]);

  return (
    <div className="waveform-player" onClick={(event) => event.stopPropagation()}>
      <button className="icon-button tiny" onClick={() => void waveRef.current?.playPause()} disabled={!ready || Boolean(error)} title={label}>
        {isPlaying ? <Pause size={13} /> : <Play size={13} />}
      </button>
      <div className="waveform-canvas" ref={containerRef} aria-label={label} />
      {error && <span className="waveform-error">{error}</span>}
    </div>
  );
}
