import { Check, ChevronDown } from "lucide-react";
import {
  useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectGroup {
  label: string;
  options: SelectOption[];
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void | Promise<unknown>;
  options?: SelectOption[];
  groups?: SelectGroup[];
  label?: string;
  title?: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

interface Position {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
}

// A native <select> opens the operating system's menu, which cannot follow the app's visual language.
// This keeps native keyboard semantics but renders the menu in a portal so drawers and dialogs never clip it.
export function Select({
  value, onChange, options, groups, label, title, placeholder = "选择…", disabled = false, className = "",
}: SelectProps) {
  const baseId = useId().replace(/:/g, "");
  const listId = `${baseId}-listbox`;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [position, setPosition] = useState<Position | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const optionNodes = useRef(new Map<number, HTMLButtonElement>());
  const typed = useRef({ text: "", at: 0 });
  const items = useMemo(() => groups ? groups.flatMap((g) => g.options) : options ?? [], [groups, options]);
  const selected = items.find((item) => item.value === value);
  const muted = !selected || (selected.disabled && selected.value === "");
  const firstEnabled = (from = 1): number => {
    if (!items.length) return -1;
    for (let n = 0; n < items.length; n++) {
      const index = ((from - 1 + n) % items.length + items.length) % items.length;
      if (!items[index].disabled) return index;
    }
    return -1;
  };

  const place = useCallback(() => {
    const el = trigger.current;
    if (!el) return;
    // Composer pills wrap a label and icon around the trigger, so align the menu to the whole pill.
    const rect = (el.closest(".composer-pill") ?? el).getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const below = window.innerHeight - rect.bottom - gap - margin;
    const above = rect.top - gap - margin;
    const up = below < 210 && above > below;
    const width = Math.min(Math.max(rect.width, 196), window.innerWidth - margin * 2);
    setPosition({
      left: Math.min(Math.max(margin, rect.left), window.innerWidth - margin - width),
      width,
      maxHeight: Math.min(320, Math.max(96, up ? above : below)),
      ...(up ? { bottom: window.innerHeight - rect.top + gap } : { top: rect.bottom + gap }),
    });
  }, []);

  const close = useCallback((restore: boolean) => {
    setOpen(false);
    setPosition(null);
    if (restore) window.requestAnimationFrame(() => trigger.current?.focus());
  }, []);

  const openMenu = useCallback((index?: number) => {
    if (disabled) return;
    place();
    if (index !== undefined) setActive(index);
    else {
      const current = items.findIndex((item) => item.value === value && !item.disabled);
      const fallback = firstEnabled();
      setActive(current >= 0 ? current : Math.max(0, fallback));
    }
    setOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disabled, items, value, place, firstEnabled]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (trigger.current?.contains(target) || menu.current?.contains(target)) return;
      close(false);
    };
    const onFocus = (event: FocusEvent) => {
      const target = event.target as Node;
      if (trigger.current?.contains(target) || menu.current?.contains(target)) return;
      close(false);
    };
    const onViewport = () => place();
    document.addEventListener("pointerdown", onPointer, true);
    document.addEventListener("focusin", onFocus, true);
    window.addEventListener("resize", onViewport);
    window.addEventListener("scroll", onViewport, true);
    return () => {
      document.removeEventListener("pointerdown", onPointer, true);
      document.removeEventListener("focusin", onFocus, true);
      window.removeEventListener("resize", onViewport);
      window.removeEventListener("scroll", onViewport, true);
    };
  }, [open, close, place]);

  useLayoutEffect(() => {
    if (!open) return;
    const el = menu.current;
    const node = optionNodes.current.get(active);
    if (!el || !node) return;
    if (node.offsetTop < el.scrollTop) el.scrollTop = node.offsetTop;
    else if (node.offsetTop + node.offsetHeight > el.scrollTop + el.clientHeight)
      el.scrollTop = node.offsetTop + node.offsetHeight - el.clientHeight;
  }, [open, active]);

  const choose = (index: number) => {
    const item = items[index];
    if (!item || item.disabled) return;
    if (item.value !== value) void onChange(item.value);
    close(true);
  };

  const move = (direction: 1 | -1) => {
    if (!items.length) return;
    let index = active;
    for (let n = 0; n < items.length; n++) {
      index = (index + direction + items.length) % items.length;
      if (!items[index].disabled) { setActive(index); return; }
    }
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openMenu();
      else move(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      const start = event.key === "Home" ? 1 : items.length;
      const direction = event.key === "Home" ? 1 : -1;
      let index = start - 1;
      for (let n = 0; n < items.length; n++) {
        index = (index + direction + items.length) % items.length;
        if (!items[index].disabled) { setActive(index); break; }
      }
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!open) openMenu();
      else choose(active);
      return;
    }
    if (event.key === "Escape") {
      if (open) { event.preventDefault(); close(true); }
      return;
    }
    if (event.key === "Tab") {
      if (open) close(false);
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || event.key.length !== 1) return;
    const now = Date.now();
    const text = now - typed.current.at > 600 ? event.key : typed.current.text + event.key;
    typed.current = { text, at: now };
    const match = items.findIndex((item) => !item.disabled && item.label.toLowerCase().startsWith(text.toLowerCase()));
    if (match >= 0) {
      event.preventDefault();
      if (open) setActive(match);
      else openMenu(match);
    }
  };

  const option = (item: SelectOption, index: number) => (
    <button
      key={item.value}
      ref={(node) => { if (node) optionNodes.current.set(index, node); else optionNodes.current.delete(index); }}
      type="button"
      role="option"
      id={`${baseId}-opt-${index}`}
      aria-selected={item.value === value}
      aria-disabled={item.disabled || undefined}
      tabIndex={-1}
      className={`select-option${index === active ? " active" : ""}${item.disabled ? " disabled" : ""}`}
      onMouseEnter={() => { if (!item.disabled) setActive(index); }}
      onClick={() => choose(index)}
    >
      <span className="select-option-label">{item.label}</span>
      {item.value === value && <Check size={13} className="select-check" aria-hidden />}
    </button>
  );

  let optionIndex = 0;

  return (
    <div className={`select${disabled ? " disabled" : ""}${className ? ` ${className}` : ""}`}>
      <button
        ref={trigger}
        type="button"
        className="select-trigger"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && active >= 0 ? `${baseId}-opt-${active}` : undefined}
        aria-haspopup="listbox"
        title={title ?? label}
        disabled={disabled}
        onClick={() => open ? close(true) : openMenu()}
        onKeyDown={onKeyDown}
      >
        <span className={`select-value${muted ? " placeholder" : ""}`}>{selected?.label ?? (value || placeholder)}</span>
        <ChevronDown size={13} className="select-chevron" aria-hidden />
      </button>
      {open && position && createPortal(
        <div
          ref={menu}
          id={listId}
          className="select-menu"
          role="listbox"
          aria-label={label}
          style={{ left: position.left, width: position.width, maxHeight: position.maxHeight,
            top: position.top, bottom: position.bottom }}
        >
          {groups
            ? groups.map((group) => (
                <div className="select-group" role="group" aria-label={group.label} key={group.label}>
                  <div className="select-group-label" aria-hidden="true">{group.label}</div>
                  {group.options.map((item) => option(item, optionIndex++))}
                </div>
              ))
            : items.map((item, index) => option(item, index))}
        </div>,
        document.body,
      )}
    </div>
  );
}
