// WebSocket transport to a headless PandaCode core (`pandacode-core`), used when the
// page is opened with `?core=ws://host:port`. Frames are JSON:
//   client → core  {id, command, args}          a command call
//                  {subscribe: string[]}        the event names this page listens to
//   core → client  {id, ok: true, result}       a command result
//                  {id, ok: false, error}       a command error
//                  {event, payload}             an event emitted by the core
// The core keeps running when the socket drops; the transport reconnects with
// backoff, re-sends its subscriptions, and tells `onReconnect` listeners so the
// app can reload what changed while it was away.

export type Transport = {
  call<T>(command: string, args?: Record<string, unknown>): Promise<T>;
  subscribe(name: string, callback: (payload: any) => void): Promise<() => void>;
  onReconnect(callback: () => void): () => void;
  onStatus(callback: (status: TransportStatus) => void): () => void;
  close(): void;
};
export type TransportStatus = 'connecting' | 'open' | 'closed';
type Socket = Pick<WebSocket, 'send' | 'close' | 'readyState'> & {
  onopen: ((event: any) => void) | null;
  onclose: ((event: any) => void) | null;
  onerror: ((event: any) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
};
export type ConnectOptions = {
  /** Socket constructor; tests pass a fake. */
  socket?: (url: string) => Socket;
  /** Delay before reconnect attempt `n` (0-based). */
  backoff?: (attempt: number) => number;
};

const OPEN = 1;
export const backoff = (attempt: number): number => Math.min(5_000, 250 * 2 ** attempt) * (0.75 + Math.random() * 0.5);

export function connect(url: string, options: ConnectOptions = {}): Transport {
  const open = options.socket ?? (target => new WebSocket(target) as Socket);
  const delay = options.backoff ?? backoff;
  const listeners = new Map<string, Set<(payload: any) => void>>();
  const pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>();
  // Calls made while disconnected wait here (by id) and go out on the next open socket.
  const outbox = new Map<number, string>();
  const reconnected = new Set<() => void>();
  const statusListeners = new Set<(status: TransportStatus) => void>();
  let socket: Socket | null = null;
  let nextId = 1, attempt = 0, everOpened = false, closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const setStatus = (status: TransportStatus) => statusListeners.forEach(callback => callback(status));
  const sendSubscriptions = () => {
    if (socket?.readyState === OPEN) socket.send(JSON.stringify({ subscribe: [...listeners.keys()] }));
  };
  const dial = () => {
    if (closed) return;
    setStatus('connecting');
    const current = socket = open(url);
    current.onopen = () => {
      if (current !== socket) return;
      attempt = 0;
      sendSubscriptions();
      for (const frame of outbox.values()) current.send(frame);
      outbox.clear();
      setStatus('open');
      if (everOpened) reconnected.forEach(callback => callback());
      everOpened = true;
    };
    current.onmessage = ({ data }) => {
      let frame: any;
      try { frame = JSON.parse(String(data)); } catch { return; }
      if (frame && typeof frame.event === 'string') {
        for (const callback of [...listeners.get(frame.event) ?? []]) callback(frame.payload);
      } else if (frame && typeof frame.id === 'number') {
        const call = pending.get(frame.id);
        if (!call) return;
        pending.delete(frame.id);
        if (frame.ok) call.resolve(frame.result);
        else call.reject(new Error(typeof frame.error === 'string' ? frame.error : JSON.stringify(frame.error)));
      }
    };
    current.onerror = () => {};
    current.onclose = () => {
      if (current !== socket) return;
      socket = null;
      // A call already sent may or may not have run on the core; never replay it.
      // Calls still in the outbox were never sent and wait for the next socket.
      for (const [id, call] of pending) {
        if (outbox.has(id)) continue;
        pending.delete(id);
        call.reject(new Error('Lost the connection to the PandaCode core.'));
      }
      setStatus('closed');
      if (!closed) timer = setTimeout(dial, delay(attempt++));
    };
  };

  dial();
  return {
    call<T>(command: string, args: Record<string, unknown> = {}): Promise<T> {
      if (closed) return Promise.reject(new Error('The PandaCode core connection is closed.'));
      const id = nextId++;
      const frame = JSON.stringify({ id, command, args });
      return new Promise<T>((resolve, reject) => {
        pending.set(id, { resolve, reject });
        if (socket?.readyState === OPEN) socket.send(frame);
        else outbox.set(id, frame);
      });
    },
    async subscribe(name, callback) {
      let set = listeners.get(name);
      const added = !set;
      if (!set) listeners.set(name, set = new Set());
      set.add(callback);
      if (added) sendSubscriptions();
      return () => {
        const current = listeners.get(name);
        if (!current?.delete(callback) || current.size) return;
        listeners.delete(name);
        sendSubscriptions();
      };
    },
    onReconnect(callback) { reconnected.add(callback); return () => reconnected.delete(callback); },
    onStatus(callback) { statusListeners.add(callback); return () => statusListeners.delete(callback); },
    close() {
      closed = true;
      clearTimeout(timer);
      socket?.close();
      for (const call of pending.values()) call.reject(new Error('The PandaCode core connection is closed.'));
      pending.clear();
      outbox.clear();
    },
  };
}
