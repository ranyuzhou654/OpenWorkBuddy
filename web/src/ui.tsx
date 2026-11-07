import { AnimatePresence, motion } from "motion/react";
import {
  createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";

/* ------------------------------------------------------------------ */
/* Toasts and confirmation dialogs                                     */
/* ------------------------------------------------------------------ */

type ToastKind = "error" | "success" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

export interface ConfirmOptions {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface ConfirmRequest extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

interface UiApi {
  toast: (kind: ToastKind, message: string) => void;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
}

const UiContext = createContext<UiApi | null>(null);

let scrollLocks = 0;
function lockScroll() {
  if (scrollLocks++ === 0) document.body.style.overflow = "hidden";
}
function unlockScroll() {
  scrollLocks = Math.max(0, scrollLocks - 1);
  if (scrollLocks === 0) document.body.style.overflow = "";
}

export function UIProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<number, number>());
  const nextId = useRef(1);
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const requestRef = useRef<ConfirmRequest | null>(null);

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer) window.clearTimeout(timer);
    timers.current.delete(id);
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback((kind: ToastKind, message: string) => {
    const id = nextId.current++;
    setToasts((list) => [...list.slice(-3), { id, kind, message }]);
    const timer = window.setTimeout(() => {
      timers.current.delete(id);
      setToasts((list) => list.filter((t) => t.id !== id));
    }, kind === "error" ? 7000 : 4000);
    timers.current.set(id, timer);
  }, []);

  const confirm = useCallback((options: ConfirmOptions) => new Promise<boolean>((resolve) => {
    if (requestRef.current) {
      resolve(false);
      return;
    }
    const req = { ...options, resolve };
    requestRef.current = req;
    setRequest(req);
  }), []);

  const settle = useCallback((ok: boolean) => {
    const req = requestRef.current;
    requestRef.current = null;
    setRequest(null);
    req?.resolve(ok);
  }, []);

  useEffect(() => () => {
    for (const timer of timers.current.values()) window.clearTimeout(timer);
    timers.current.clear();
  }, []);

  const api = useMemo(() => ({ toast, confirm }), [toast, confirm]);

  return (
    <UiContext.Provider value={api}>
      {children}
      <div className="toast-viewport" role="region" aria-label="通知">
        <AnimatePresence initial={false}>
          {toasts.map((t) => (
            <motion.div key={t.id} layout className={`toast ${t.kind}`}
              role={t.kind === "error" ? "alert" : "status"} aria-atomic="true"
              initial={{ opacity: 0, y: 14, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }} transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}>
              <span className="toast-icon" aria-hidden>
                {t.kind === "error" ? <AlertCircle size={16} /> : t.kind === "success" ? <CheckCircle2 size={16} /> : <Info size={16} />}
              </span>
              <span className="toast-message">{t.message}</span>
              <button className="toast-close" aria-label="关闭通知" onClick={() => dismiss(t.id)}><X size={13} /></button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
      <Dialog open={request !== null} onClose={() => settle(false)}
        title={request?.title ?? ""} size="sm" className="confirm-dialog">
        <div className="confirm-message">{request?.message}</div>
        <div className="dialog-actions">
          <button onClick={() => settle(false)}>{request?.cancelLabel ?? "取消"}</button>
          <button className={request?.danger ? "danger-solid" : "primary"} autoFocus
            onClick={() => settle(true)}>{request?.confirmLabel ?? "确认"}</button>
        </div>
      </Dialog>
    </UiContext.Provider>
  );
}

function useUi(): UiApi {
  const value = useContext(UiContext);
  if (!value) throw new Error("UIProvider is missing");
  return value;
}

export function useToast() {
  const { toast } = useUi();
  return useMemo(() => ({
    show: toast,
    error: (message: unknown) => toast("error", typeof message === "string" ? message : String(message)),
    success: (message: string) => toast("success", message),
    info: (message: string) => toast("info", message),
  }), [toast]);
}

export function useConfirm() {
  return useUi().confirm;
}

/* ------------------------------------------------------------------ */
/* Dialog                                                              */
/* ------------------------------------------------------------------ */

export const FOCUSABLE = "a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function Dialog({ open, onClose, title, description, children, footer, size = "md", className = "",
  hideClose = false }: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
  hideClose?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const restore = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    if (!open) return;
    restore.current = document.activeElement as HTMLElement | null;
    lockScroll();
    const timer = window.setTimeout(() => {
      const first = panel.current?.querySelector<HTMLElement>("[data-autofocus], " + FOCUSABLE);
      (first ?? panel.current)?.focus();
    }, 30);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const focusable = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
        .filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (!focusable.length) {
        event.preventDefault();
        panel.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKey, true);
      unlockScroll();
      restore.current?.focus?.();
    };
  }, [open]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="dialog-layer" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
          <motion.div ref={panel} role="dialog" aria-modal="true" tabIndex={-1}
            aria-labelledby={title ? titleId : undefined} aria-describedby={description ? descId : undefined}
            className={`dialog-panel ${size} ${className}`}
            initial={{ opacity: 0, y: 10, scale: 0.985 }} animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 5, scale: 0.992 }} transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}>
            {(title || !hideClose) && (
              <div className="dialog-head">
                <div>
                  {title && <h2 id={titleId}>{title}</h2>}
                  {description && <p id={descId} className="dialog-desc">{description}</p>}
                </div>
                {!hideClose && <button className="dialog-x" aria-label="关闭" onClick={onClose}><X size={16} /></button>}
              </div>
            )}
            <div className="dialog-body">{children}</div>
            {footer && <div className="dialog-actions">{footer}</div>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

/* ------------------------------------------------------------------ */
/* Popover                                                             */
/* ------------------------------------------------------------------ */

export function usePopover() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return {
    open,
    setOpen,
    toggle: () => setOpen((value) => !value),
    close: () => setOpen(false),
    wrapRef,
    triggerRef,
  };
}

export function PopoverPanel({ open, children, className = "", align = "end", side = "bottom" }: {
  open: boolean;
  children: ReactNode;
  className?: string;
  align?: "start" | "end";
  side?: "bottom" | "top";
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div className={`popover-panel ${align} ${side} ${className}`}
          initial={{ opacity: 0, y: side === "bottom" ? -4 : 4, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: side === "bottom" ? -2 : 2, scale: 0.99 }}
          transition={{ duration: 0.14, ease: [0.22, 1, 0.36, 1] }}>
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/* ------------------------------------------------------------------ */
/* Loading and page chrome                                             */
/* ------------------------------------------------------------------ */

export function Skeleton({ lines = 3, card = false }: { lines?: number; card?: boolean }) {
  const body = (
    <div className="skeleton" aria-hidden>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="skel-line" style={{ width: `${[92, 78, 86, 64, 74][i % 5]}%` }} />
      ))}
    </div>
  );
  return card ? <div className="card skel-card">{body}</div> : body;
}

export function SkeletonList({ count = 3 }: { count?: number }) {
  return (
    <div className="skeleton-list" aria-label="加载中" role="status">
      {Array.from({ length: count }, (_, i) => <Skeleton key={i} card lines={3} />)}
    </div>
  );
}

export function PageHeader({ eyebrow, title, description, actions, icon }: {
  eyebrow?: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-heading">
        {eyebrow && <div className="page-eyebrow">{eyebrow}</div>}
        <h1>{icon}{title}</h1>
        {description && <p className="page-desc">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
