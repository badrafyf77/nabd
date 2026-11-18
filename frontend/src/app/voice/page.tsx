"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_VOICE_SERVER_URL =
  process.env.NEXT_PUBLIC_VOICE_SERVER_URL || "http://localhost:8080";
const SERVER_READY_TIMEOUT_MS = 60_000;
const SOCKET_CONNECT_TIMEOUT_MS = 10_000;
const AUDIO_BATCH_SAMPLES = 2048;
const AUDIO_HEADER_BYTES = 8;
const AUDIO_MESSAGE_BYTES = AUDIO_HEADER_BYTES + AUDIO_BATCH_SAMPLES * 2;

type TranscriptMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  type: "partial" | "final";
  finishComplete?: boolean;
  success?: boolean;
  error?: string;
};

type WebSocketPayload = {
  type: string;
  status?: string;
  message?: string;
  content?: string;
  success?: boolean;
  error?: string;
};

function normalizeServerUrl(value: string) {
  const trimmedValue = value.trim() || DEFAULT_VOICE_SERVER_URL;
  return /^(https?|wss?):\/\//i.test(trimmedValue)
    ? trimmedValue
    : `http://${trimmedValue}`;
}

function buildWebSocketUrl(serverUrl: string) {
  const baseUrl = new URL(normalizeServerUrl(serverUrl));

  if (baseUrl.protocol === "http:") {
    baseUrl.protocol = "ws:";
  } else if (baseUrl.protocol === "https:") {
    baseUrl.protocol = "wss:";
  }

  baseUrl.pathname = "/ws";
  baseUrl.search = "";
  baseUrl.hash = "";
  return baseUrl.toString();
}

function base64ToInt16Array(value: string) {
  const rawValue = atob(value);
  const bytes = new Uint8Array(rawValue.length);

  for (let index = 0; index < rawValue.length; index += 1) {
    bytes[index] = rawValue.charCodeAt(index);
  }

  return new Int16Array(bytes.buffer);
}

function createTranscriptMessage(
  role: TranscriptMessage["role"],
  content: string,
  type: TranscriptMessage["type"],
): TranscriptMessage {
  return {
    id: `${role}-${type}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    role,
    content,
    type,
  };
}

function upsertUserMessage(
  previousMessages: TranscriptMessage[],
  content: string,
  type: TranscriptMessage["type"],
) {
  if (!content.trim()) {
    return previousMessages;
  }

  const lastUserIndex = previousMessages.findLastIndex(
    (message) => message.role === "user",
  );
  const nextMessage = createTranscriptMessage("user", content, type);

  if (
    lastUserIndex === -1 ||
    previousMessages[lastUserIndex].type !== "partial"
  ) {
    return [...previousMessages, nextMessage];
  }

  const updatedMessages = [...previousMessages];
  updatedMessages[lastUserIndex] = nextMessage;
  return updatedMessages;
}

function waitForReady(isReadyRef: { current: boolean }) {
  return new Promise<void>((resolve, reject) => {
    const startedAt = Date.now();

    const check = () => {
      if (isReadyRef.current) {
        resolve();
        return;
      }

      if (Date.now() - startedAt >= SERVER_READY_TIMEOUT_MS) {
        reject(
          new Error("The voice agent server did not become ready in time."),
        );
        return;
      }

      window.setTimeout(check, 100);
    };

    check();
  });
}

function useVoiceSocket(serverUrl: string) {
  const [isConnected, setIsConnected] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [serverStatus, setServerStatus] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [isTtsPlaying, setIsTtsPlaying] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const ttsWorkletNodeRef = useRef<AudioWorkletNode | null>(null);
  const connectionPromiseRef = useRef<Promise<void> | null>(null);

  const initializeAudioPlayback = useCallback(async () => {
    if (audioContextRef.current && ttsWorkletNodeRef.current) {
      return;
    }

    const audioContext = new AudioContext({ sampleRate: 24_000 });
    await audioContext.audioWorklet.addModule(
      "/static/ttsPlaybackProcessor.js",
    );
    await audioContext.resume();

    const ttsWorkletNode = new AudioWorkletNode(
      audioContext,
      "tts-playback-processor",
    );
    ttsWorkletNode.port.onmessage = ({ data }) => {
      if (data.type === "ttsPlaybackStarted") {
        setIsTtsPlaying(true);
        socketRef.current?.send(JSON.stringify({ type: "tts_start" }));
      }

      if (data.type === "ttsPlaybackStopped") {
        setIsTtsPlaying(false);
        socketRef.current?.send(JSON.stringify({ type: "tts_stop" }));
      }
    };

    ttsWorkletNode.connect(audioContext.destination);
    audioContextRef.current = audioContext;
    ttsWorkletNodeRef.current = ttsWorkletNode;
  }, []);

  const disconnect = useCallback(() => {
    connectionPromiseRef.current = null;

    socketRef.current?.close();
    socketRef.current = null;

    ttsWorkletNodeRef.current?.disconnect();
    ttsWorkletNodeRef.current = null;

    void audioContextRef.current?.close();
    audioContextRef.current = null;

    setIsConnected(false);
    setIsReady(false);
    setServerStatus("");
    setStatusMessage("");
    setIsTtsPlaying(false);
  }, []);

  const handleJsonMessage = useCallback((payload: WebSocketPayload) => {
    switch (payload.type) {
      case "status":
        setServerStatus(payload.status || "");
        setStatusMessage(payload.message || "");
        setIsReady(payload.status === "ready");
        break;

      case "partial_user_request":
        setMessages((previousMessages) =>
          payload.content
            ? upsertUserMessage(previousMessages, payload.content, "partial")
            : previousMessages,
        );
        break;

      case "final_user_request":
        setMessages((previousMessages) =>
          payload.content
            ? upsertUserMessage(previousMessages, payload.content, "final")
            : previousMessages,
        );
        break;

      case "assistant_partial":
      case "partial_assistant_answer":
      case "assistant_sentence":
        setMessages((previousMessages) => {
          if (!payload.content?.trim()) {
            return previousMessages;
          }

          const lastAssistantIndex = previousMessages.findLastIndex(
            (message) =>
              message.role === "assistant" && message.type === "partial",
          );

          if (lastAssistantIndex === -1) {
            return [
              ...previousMessages,
              createTranscriptMessage("assistant", payload.content, "partial"),
            ];
          }

          const updatedMessages = [...previousMessages];
          const previousContent = updatedMessages[lastAssistantIndex].content;
          updatedMessages[lastAssistantIndex] = {
            ...updatedMessages[lastAssistantIndex],
            content:
              payload.type === "assistant_sentence" &&
              !previousContent.includes(payload.content)
                ? `${previousContent}${payload.content}`
                : payload.content,
          };

          return updatedMessages;
        });
        break;

      case "final_assistant_answer":
        setMessages((previousMessages) => {
          if (!payload.content?.trim()) {
            return previousMessages;
          }

          const lastAssistantIndex = previousMessages.findLastIndex(
            (message) =>
              message.role === "assistant" && message.type === "partial",
          );

          if (lastAssistantIndex === -1) {
            return [
              ...previousMessages,
              createTranscriptMessage("assistant", payload.content, "final"),
            ];
          }

          const updatedMessages = [...previousMessages];
          updatedMessages[lastAssistantIndex] = {
            ...updatedMessages[lastAssistantIndex],
            content: payload.content,
            type: "final",
          };
          return updatedMessages;
        });
        break;

      case "tts_chunk":
        if (ttsWorkletNodeRef.current && payload.content) {
          ttsWorkletNodeRef.current.port.postMessage(
            base64ToInt16Array(payload.content),
          );
        }
        break;

      case "tts_interruption":
      case "stop_tts":
        ttsWorkletNodeRef.current?.port.postMessage({ type: "clear" });
        setIsTtsPlaying(false);
        break;

      case "finish_complete":
        setMessages((previousMessages) => [
          ...previousMessages,
          {
            ...createTranscriptMessage(
              "assistant",
              payload.error || "Voice chat ended.",
              "final",
            ),
            finishComplete: true,
            success: payload.success,
            error: payload.error,
          },
        ]);
        break;

      default:
        break;
    }
  }, []);

  const connect = useCallback(async () => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    if (connectionPromiseRef.current) {
      return connectionPromiseRef.current;
    }

    await initializeAudioPlayback();

    connectionPromiseRef.current = new Promise<void>((resolve, reject) => {
      let settled = false;
      const socket = new WebSocket(buildWebSocketUrl(serverUrl));

      const timeoutId = window.setTimeout(() => {
        socket.close();
        rejectOnce(
          new Error("Timed out while connecting to the voice chat server."),
        );
      }, SOCKET_CONNECT_TIMEOUT_MS);

      const cleanup = () => {
        window.clearTimeout(timeoutId);
        connectionPromiseRef.current = null;
      };

      const resolveOnce = () => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        resolve();
      };

      const rejectOnce = (error: Error) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        reject(error);
      };

      socket.onopen = () => {
        setIsConnected(true);
        setIsReady(false);
        setStatusMessage("Connecting...");
        resolveOnce();
      };

      socket.onmessage = (event) => {
        if (typeof event.data !== "string") {
          return;
        }

        try {
          handleJsonMessage(JSON.parse(event.data) as WebSocketPayload);
        } catch (error) {
          console.error("Error parsing voice-agent socket message:", error);
        }
      };

      socket.onclose = () => {
        setIsConnected(false);
        setIsReady(false);
        setStatusMessage("");

        if (!settled) {
          rejectOnce(new Error("The voice chat server closed the connection."));
        }
      };

      socket.onerror = () => {
        setIsConnected(false);
        rejectOnce(new Error("Unable to connect to the voice chat server."));
      };

      socketRef.current = socket;
    });

    return connectionPromiseRef.current;
  }, [handleJsonMessage, initializeAudioPlayback, serverUrl]);

  const sendAudioData = useCallback((audioBuffer: ArrayBuffer) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(audioBuffer);
    }
  }, []);

  const sendMessage = useCallback((message: Record<string, unknown>) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify(message));
    }
  }, []);

  const clearHistory = useCallback(() => {
    setMessages([]);
    sendMessage({ type: "clear_history" });
  }, [sendMessage]);

  const stopTts = useCallback(() => {
    ttsWorkletNodeRef.current?.port.postMessage({ type: "clear" });
    setIsTtsPlaying(false);
  }, []);

  useEffect(() => disconnect, [disconnect]);

  return {
    isConnected,
    isReady,
    serverStatus,
    statusMessage,
    messages,
    connect,
    disconnect,
    clearHistory,
    sendAudioData,
    stopTts,
    isTtsPlaying,
  };
}

function useAudioCapture(
  onAudioData: (data: ArrayBuffer) => void,
  getTtsPlayingState: () => boolean,
) {
  const [isCapturing, setIsCapturing] = useState(false);
  // off -> asking (permission prompt) -> on, or blocked when it can't be used.
  const [micState, setMicState] = useState<MicState>("off");
  // True when the browser hasn't answered the permission request for a while.
  const [micSlow, setMicSlow] = useState(false);
  // Loudness of the last ~0.2 s of microphone input, 0 to 1, to show it works.
  const [micLevel, setMicLevel] = useState(0);
  const levelPeakRef = useRef(0);
  const levelUpdatedAtRef = useRef(0);
  const audioContextRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const batchBufferRef = useRef<ArrayBuffer | null>(null);
  const batchViewRef = useRef<DataView | null>(null);
  const batchInt16Ref = useRef<Int16Array | null>(null);
  const batchOffsetRef = useRef(0);
  const bufferPoolRef = useRef<ArrayBuffer[]>([]);

  const initializeBatch = useCallback(() => {
    if (batchBufferRef.current) {
      return;
    }

    batchBufferRef.current =
      bufferPoolRef.current.pop() || new ArrayBuffer(AUDIO_MESSAGE_BYTES);
    batchViewRef.current = new DataView(batchBufferRef.current);
    batchInt16Ref.current = new Int16Array(
      batchBufferRef.current,
      AUDIO_HEADER_BYTES,
    );
    batchOffsetRef.current = 0;
  }, []);

  const flushBatch = useCallback(() => {
    if (!batchViewRef.current || !batchBufferRef.current) {
      return;
    }

    const buffer = batchBufferRef.current;
    batchViewRef.current.setUint32(0, Date.now() & 0xffffffff, false);
    batchViewRef.current.setUint32(4, getTtsPlayingState() ? 1 : 0, false);
    onAudioData(buffer);

    bufferPoolRef.current.push(buffer);
    batchBufferRef.current = null;
    batchViewRef.current = null;
    batchInt16Ref.current = null;
    batchOffsetRef.current = 0;
  }, [getTtsPlayingState, onAudioData]);

  const flushRemainder = useCallback(() => {
    if (batchOffsetRef.current === 0 || !batchInt16Ref.current) {
      return;
    }

    for (
      let index = batchOffsetRef.current;
      index < AUDIO_BATCH_SAMPLES;
      index += 1
    ) {
      batchInt16Ref.current[index] = 0;
    }

    flushBatch();
  }, [flushBatch]);

  const startCapture = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setMicState("blocked");
      throw new Error(
        "This browser won't allow the microphone on this address. Open the page at http://localhost:3000 (not the network 192.168… address), or over HTTPS.",
      );
    }

    // Create and start the audio context first, while we are still inside the
    // Start click: some browsers (Safari) only allow audio to start from a click.
    const audioContext = new AudioContext({ sampleRate: 48_000 });
    audioContextRef.current = audioContext;
    void audioContext.resume();

    setMicState("asking");
    setMicSlow(false);
    // Some built-in browsers (the Claude app's browser pane, editor previews)
    // never answer: no prompt, no error. Say so instead of waiting silently.
    const slowTimer = window.setTimeout(() => setMicSlow(true), 6_000);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: { ideal: 48_000 },
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
    } catch (error) {
      window.clearTimeout(slowTimer);
      setMicSlow(false);
      setMicState("blocked");
      void audioContext.close();
      audioContextRef.current = null;
      const name = error instanceof DOMException ? error.name : "";
      throw new Error(
        name === "NotAllowedError"
          ? "The microphone is blocked. Allow it for this page (the icon in the address bar), and on macOS also in System Settings > Privacy & Security > Microphone for your browser."
          : name === "NotFoundError"
            ? "No microphone was found. Plug one in or pick one in your system sound settings."
            : `Could not open the microphone: ${error instanceof Error ? error.message : String(error)}`,
      );
    }

    window.clearTimeout(slowTimer);
    setMicSlow(false);
    streamRef.current = stream;

    await audioContext.audioWorklet.addModule("/static/pcmWorkletProcessor.js");
    await audioContext.resume();

    const workletNode = new AudioWorkletNode(
      audioContext,
      "pcm-worklet-processor",
    );
    workletNode.port.onmessage = ({ data }) => {
      const incoming = new Int16Array(data);
      let readOffset = 0;

      for (let index = 0; index < incoming.length; index += 1) {
        const value = Math.abs(incoming[index]);
        if (value > levelPeakRef.current) {
          levelPeakRef.current = value;
        }
      }
      const now = performance.now();
      if (now - levelUpdatedAtRef.current > 200) {
        setMicLevel(Math.min(1, levelPeakRef.current / 16_000));
        levelPeakRef.current = 0;
        levelUpdatedAtRef.current = now;
      }

      while (readOffset < incoming.length) {
        initializeBatch();

        const copyLength = Math.min(
          incoming.length - readOffset,
          AUDIO_BATCH_SAMPLES - batchOffsetRef.current,
        );
        batchInt16Ref.current?.set(
          incoming.subarray(readOffset, readOffset + copyLength),
          batchOffsetRef.current,
        );

        batchOffsetRef.current += copyLength;
        readOffset += copyLength;

        if (batchOffsetRef.current === AUDIO_BATCH_SAMPLES) {
          flushBatch();
        }
      }
    };

    audioContext.createMediaStreamSource(stream).connect(workletNode);
    workletNodeRef.current = workletNode;
    setIsCapturing(true);
    setMicState("on");
  }, [flushBatch, initializeBatch]);

  const stopCapture = useCallback(() => {
    flushRemainder();

    workletNodeRef.current?.disconnect();
    workletNodeRef.current = null;

    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;

    void audioContextRef.current?.close();
    audioContextRef.current = null;

    setIsCapturing(false);
    setMicState((state) => (state === "blocked" ? state : "off"));
    setMicLevel(0);
  }, [flushRemainder]);

  return {
    isCapturing,
    micState,
    micSlow,
    micLevel,
    startCapture,
    stopCapture,
  };
}

type MicState = "off" | "asking" | "on" | "blocked";

function micStatusText(micState: MicState, isRecording: boolean, serverStatus: string) {
  if (micState === "asking") {
    return "Waiting for microphone permission. Check your browser's address bar.";
  }
  if (micState === "blocked") {
    return "Microphone blocked.";
  }
  if (micState === "on") {
    return isRecording
      ? "Microphone is on. The bar moves when you speak."
      : "Microphone is on. Connecting...";
  }
  return serverStatus || "Ready to connect.";
}

function transcriptLabel(message: TranscriptMessage) {
  return message.role === "assistant" ? "Assistant" : "You";
}

export default function Home() {
  const [serverUrl, setServerUrl] = useState(DEFAULT_VOICE_SERVER_URL);
  const [isStarting, setIsStarting] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");

  const isReadyRef = useRef(false);
  const isTtsPlayingRef = useRef(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setServerUrl(params.get("server") || DEFAULT_VOICE_SERVER_URL);
  }, []);

  const {
    isConnected,
    isReady,
    serverStatus,
    statusMessage,
    messages,
    connect,
    disconnect,
    clearHistory,
    sendAudioData,
    stopTts,
    isTtsPlaying,
  } = useVoiceSocket(serverUrl);

  const { micState, micSlow, micLevel, startCapture, stopCapture } = useAudioCapture(
    sendAudioData,
    () => isTtsPlayingRef.current,
  );

  useEffect(() => {
    isReadyRef.current = isReady;
  }, [isReady]);

  useEffect(() => {
    isTtsPlayingRef.current = isTtsPlaying;
  }, [isTtsPlaying]);

  useEffect(() => {
    const finishMessage = [...messages]
      .reverse()
      .find((message) => message.finishComplete);
    if (!finishMessage) {
      return;
    }

    stopCapture();
    setIsRecording(false);

    if (!finishMessage.success) {
      setErrorMessage(
        finishMessage.error || "The voice chat could not be ended cleanly.",
      );
    }
  }, [messages, stopCapture]);

  const stopSession = useCallback(() => {
    stopTts();
    stopCapture();
    disconnect();
    setIsRecording(false);
    setIsStarting(false);
  }, [disconnect, stopCapture, stopTts]);

  const startSession = useCallback(async () => {
    setErrorMessage("");
    setIsStarting(true);

    try {
      // Ask for the microphone right away (inside the click), while connecting.
      await Promise.all([startCapture(), connect()]);
      await waitForReady(isReadyRef);
      setIsRecording(true);
    } catch (error) {
      stopSession();
      setErrorMessage(
        error instanceof Error
          ? error.message
          : "Could not start the voice agent.",
      );
    } finally {
      setIsStarting(false);
    }
  }, [connect, startCapture, stopSession]);

  const resetTranscript = useCallback(() => {
    clearHistory();
    setErrorMessage("");
  }, [clearHistory]);

  const canStart = !isConnected && !isStarting;
  const currentStatus =
    statusMessage || (isConnected ? "Connected" : "Disconnected");
  const activityStatus = isTtsPlaying
    ? "Speaking"
    : isRecording
      ? "Listening"
      : "Idle";

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Voice Agent</p>
          <h1>Voice Chat</h1>
        </div>
        <div className="status-pill">
          <span
            className={`status-dot ${isReady ? "ready" : isConnected ? "connecting" : ""}`}
          />
          <span>{currentStatus}</span>
        </div>
      </header>

      <section className="workspace">
        <aside className="controls-panel">
          <label>
            <span>Server</span>
            <input
              value={serverUrl}
              onChange={(event) => setServerUrl(event.target.value)}
              disabled={isConnected}
            />
          </label>

          {errorMessage ? (
            <div className="error-box">{errorMessage}</div>
          ) : null}

          <div className="button-grid">
            <button
              type="button"
              className="primary-button"
              onClick={startSession}
              disabled={!canStart}
            >
              {isStarting ? "Starting..." : "Start"}
            </button>
            <button type="button" onClick={stopSession} disabled={!isConnected}>
              Stop
            </button>
            <button
              type="button"
              className="reset-button"
              onClick={resetTranscript}
            >
              Reset Transcript
            </button>
          </div>

          <div className="mic-status">
            <span className={`mic-dot ${micState === "on" ? "active" : ""}`} />
            <span>{micStatusText(micState, isRecording, serverStatus)}</span>
          </div>
          {micState === "asking" && micSlow ? (
            <div className="error-box">
              The browser hasn&apos;t answered the microphone request. If this page is
              open inside an app&apos;s built-in browser (like the Claude app&apos;s browser
              pane or an editor preview), open http://localhost:3000 in Chrome,
              Safari or Firefox instead. On macOS, also check System Settings &gt;
              Privacy &amp; Security &gt; Microphone for your browser.
            </div>
          ) : null}
          {micState === "on" ? (
            <div className="mic-level" aria-label="Microphone level">
              <div
                className="mic-level-fill"
                style={{ width: `${Math.round(micLevel * 100)}%` }}
              />
            </div>
          ) : null}
        </aside>

        <section className="transcript-panel">
          <div className="transcript-header">
            <h2>Transcript</h2>
            <span>{activityStatus}</span>
          </div>

          <div className="transcript">
            {messages.length === 0 ? (
              <p className="empty-state">Conversation will appear here.</p>
            ) : (
              messages.map((message) => (
                <article key={message.id} className={`message ${message.role}`}>
                  <div className="message-label">
                    {transcriptLabel(message)}
                  </div>
                  <p className="message-content">{message.content}</p>
                </article>
              ))
            )}
          </div>
        </section>
      </section>
    </main>
  );
}
