import { Mic2, Square, Upload } from "lucide-react";
import type { ChangeEvent, DragEvent, KeyboardEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { audioExtensionFromMimeType, isAcceptedAudioFile, pickRecordingMimeType } from "../lib/audioInput";

const RECORDING_BITS_PER_SECOND = 256_000;

interface ReferenceAudioInputProps {
  label: string;
  value?: string;
  disabled?: boolean;
  onUpload: (file: File) => Promise<void>;
}

export function ReferenceAudioInput({ label, value, disabled = false, onUpload }: ReferenceAudioInputProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        recorder.stop();
      }
      stopStream();
    };
  }, []);

  async function uploadFile(file: File) {
    if (!isAcceptedAudioFile(file)) {
      setError(t("audioInput.invalidFile"));
      return;
    }
    setError(null);
    setIsUploading(true);
    try {
      await onUpload(file);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : t("notice.referenceUploadFailed"));
    } finally {
      setIsUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    void uploadFileFromList(event.currentTarget.files);
  }

  async function uploadFileFromList(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    await uploadFile(file);
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (!disabled) setIsDragActive(true);
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragActive(false);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragActive(false);
    if (disabled) return;
    void uploadFileFromList(event.dataTransfer.files);
  }

  function openFilePicker() {
    if (!disabled && !isUploading) inputRef.current?.click();
  }

  function handleDropZoneKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openFilePicker();
  }

  async function startRecording() {
    if (disabled || isRecording || isUploading) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError(t("audioInput.unsupportedRecorder"));
      return;
    }

    try {
      setError(null);
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          autoGainControl: false,
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          sampleRate: 48_000
        }
      });
      const mimeType = pickRecordingMimeType((candidate) => MediaRecorder.isTypeSupported(candidate));
      const recorder = new MediaRecorder(stream, {
        ...(mimeType ? { mimeType } : {}),
        audioBitsPerSecond: RECORDING_BITS_PER_SECOND
      });
      chunksRef.current = [];
      streamRef.current = stream;
      recorderRef.current = recorder;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const blobType = recorder.mimeType || mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type: blobType });
        cleanupRecorder();
        setIsRecording(false);
        if (blob.size === 0) {
          setError(t("audioInput.emptyRecording"));
          return;
        }
        const file = new File([blob], `reference-recording-${Date.now()}.${audioExtensionFromMimeType(blobType)}`, { type: blobType });
        void uploadFile(file);
      };
      recorder.start(1000);
      setIsRecording(true);
    } catch {
      cleanupRecorder();
      setIsRecording(false);
      setError(t("audioInput.permissionDenied"));
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    recorder.stop();
  }

  function cleanupRecorder() {
    stopStream();
    recorderRef.current = null;
    chunksRef.current = [];
  }

  function stopStream() {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }

  const currentName = value?.split(/[\\/]/).pop();
  return (
    <div className={`reference-audio-input ${isDragActive ? "drag-active" : ""} ${isRecording ? "recording" : ""}`}>
      <div className="reference-audio-head">
        <span>{label}</span>
        {isRecording && <small><span className="recording-dot" /> {t("audioInput.recording")}</small>}
      </div>
      {value && (
        <div className="reference-audio-current">
          <span>{t("audioInput.current")}</span>
          <strong title={value}>{currentName || value}</strong>
        </div>
      )}
      <input ref={inputRef} className="sr-only" type="file" accept="audio/*" disabled={disabled || isUploading} onChange={handleFileChange} />
      <div className="reference-audio-surface">
        <div
          className="audio-drop-zone"
          onClick={openFilePicker}
          onDragLeave={handleDragLeave}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
          onKeyDown={handleDropZoneKeyDown}
          role="button"
          tabIndex={disabled ? -1 : 0}
        >
          <Upload size={14} />
          <span>{isUploading ? t("audioInput.uploading") : t("audioInput.drop")}</span>
          <small>{t("audioInput.chooseFile")}</small>
        </div>
        <div className="audio-input-actions">
          <button className="secondary-button compact-button icon-compact" type="button" disabled={disabled || isUploading} onClick={openFilePicker} title={t("audioInput.upload")}>
            <Upload size={13} />
            <span>{t("audioInput.upload")}</span>
          </button>
          <button className="secondary-button compact-button icon-compact" type="button" disabled={disabled || isUploading} onClick={() => void (isRecording ? stopRecording() : startRecording())} title={isRecording ? t("audioInput.stop") : t("audioInput.record")}>
            {isRecording ? <Square size={13} /> : <Mic2 size={13} />}
            <span>{isRecording ? t("audioInput.stop") : t("audioInput.record")}</span>
          </button>
        </div>
      </div>
      {error && <p className="reference-audio-error">{error}</p>}
    </div>
  );
}
