"""Noosphere — нативный клиент Windows (Tkinter, stdlib, без установок).

Запуск: pythonw client/noosphere.pyw  (или python client/noosphere.pyw)
Сервер: http://100.95.239.47:8300 (tailnet), токен — ~/.secrets/noosphere.token
"""
import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

SERVER_CANDIDATES = ["http://192.168.0.235:8300",   # Mac mini в домашней LAN
                     "http://100.95.239.47:8300"]   # tailnet — запасной
TOKEN_FILE = Path.home() / ".secrets" / "noosphere.token"


def pick_server() -> str:
    for url in SERVER_CANDIDATES:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    return url
        except Exception:
            continue
    return SERVER_CANDIDATES[-1]


SERVER = pick_server()

BG, BG2, FG, ACCENT, DIM = "#1e1e24", "#2a2a33", "#e8e8ec", "#4f9cf9", "#8a8a95"
TIERS = ["авто", "local", "base", "mid", "deep", "max"]
CLASSES = ["chat", "code", "extract", "summarize", "classify", "research", "explain",
           "review", "refactor", "test", "debug", "arch"]


def api(method: str, path: str, body: dict | None = None) -> object:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
    req = urllib.request.Request(
        SERVER + path, method=method,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Noosphere — {SERVER.split('//')[1]}")
        self.geometry("1100x720")
        self.configure(bg=BG)
        self.session_id = None
        self._build()
        self._refresh_sessions()
        self._refresh_status()
        if not TOKEN_FILE.exists():
            messagebox.showwarning("Noosphere", f"Нет токена: {TOKEN_FILE}")

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TButton", background=BG2, foreground=FG, borderwidth=0, padding=6)
        style.map("TButton", background=[("active", ACCENT)])
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TMenubutton", background=BG2, foreground=FG, arrowcolor=FG)

        root = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        root.pack(fill=tk.BOTH, expand=True)

        # ── слева: сессии ──
        left = ttk.Frame(root, width=260)
        root.add(left, weight=0)
        bar = ttk.Frame(left)
        bar.pack(fill=tk.X, pady=4, padx=4)
        ttk.Button(bar, text="+ Новая", command=self._new_session).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⟳", width=3, command=self._refresh_sessions).pack(side=tk.LEFT, padx=2)
        self.sessions = tk.Listbox(left, bg=BG2, fg=FG, selectbackground=ACCENT,
                                   borderwidth=0, highlightthickness=0, activestyle="none",
                                   font=("Segoe UI", 10))
        self.sessions.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.sessions.bind("<<ListboxSelect>>", self._pick_session)
        self._session_rows = []

        # ── справа: чат ──
        right = ttk.Frame(root)
        root.add(right, weight=1)
        self.chat = scrolledtext.ScrolledText(
            right, bg=BG, fg=FG, insertbackground=FG, font=("Consolas", 11),
            borderwidth=0, wrap=tk.WORD, state=tk.DISABLED, padx=10, pady=8)
        self.chat.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 2))
        self.chat.tag_config("me", foreground=ACCENT, font=("Consolas", 11, "bold"))
        self.chat.tag_config("meta", foreground=DIM, font=("Consolas", 9))
        self.chat.tag_config("err", foreground="#f26d6d")

        controls = ttk.Frame(right)
        controls.pack(fill=tk.X, padx=6)
        self.tier = tk.StringVar(value="авто")
        self.cls = tk.StringVar(value="chat")
        ttk.Label(controls, text="тир:").pack(side=tk.LEFT)
        ttk.OptionMenu(controls, self.tier, "авто", *TIERS).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(controls, text="класс:").pack(side=tk.LEFT)
        ttk.OptionMenu(controls, self.cls, "chat", *CLASSES).pack(side=tk.LEFT)

        bottom = ttk.Frame(right)
        bottom.pack(fill=tk.X, padx=6, pady=6)
        self.input = tk.Text(bottom, height=3, bg=BG2, fg=FG, insertbackground=FG,
                             font=("Segoe UI", 11), borderwidth=0, wrap=tk.WORD, padx=8, pady=6)
        self.input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.input.bind("<Control-Return>", lambda e: self._send() or "break")
        ttk.Button(bottom, text="Отправить", command=self._send).pack(side=tk.LEFT, padx=6, fill=tk.Y)

        self.status = ttk.Label(self, text="…", anchor=tk.W, font=("Segoe UI", 9))
        self.status.pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=2)

    # ── фон → UI ──
    def _bg(self, fn, done):
        def run():
            try:
                res = fn()
                self.after(0, lambda: done(res, None))
            except Exception as e:
                self.after(0, lambda: done(None, e))
        threading.Thread(target=run, daemon=True).start()

    def _say(self, text, tag=None):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, text, tag)
        self.chat.see(tk.END)
        self.chat.configure(state=tk.DISABLED)

    # ── сессии ──
    def _new_session(self):
        self.session_id = None
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state=tk.DISABLED)
        self.sessions.selection_clear(0, tk.END)
        self.input.focus_set()

    def _refresh_sessions(self):
        self._bg(lambda: api("GET", "/v1/sessions"), self._fill_sessions)

    def _fill_sessions(self, rows, err):
        if err:
            self.status.configure(text=f"сервер недоступен: {err}")
            return
        self._session_rows = rows or []
        self.sessions.delete(0, tk.END)
        for s in self._session_rows:
            mark = "●" if s.get("status") == "active" else "○"
            self.sessions.insert(tk.END, f"{mark} {s['id'][:8]}  {s.get('updated_at', '')[5:16]}")

    def _pick_session(self, _evt):
        sel = self.sessions.curselection()
        if not sel:
            return
        sid = self._session_rows[sel[0]]["id"]
        self._bg(lambda: api("GET", f"/v1/sessions/{sid}"), lambda r, e: self._fill_history(sid, r, e))

    def _fill_history(self, sid, s, err):
        if err or not s:
            self.status.configure(text=f"сессия не прочитана: {err or 'пусто'}")
            return
        self.session_id = sid
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state=tk.DISABLED)
        for t in s.get("turns", []):
            snap = t.get("snapshot") or {}
            if snap.get("prompt"):
                self._say(f"\nВы: {snap['prompt']}\n", "me")
            if snap.get("text"):
                self._say(f"{snap['text']}\n")
                self._say(f"  {snap.get('model', '?')} · {t.get('tokens_spent', 0)} tok\n", "meta")
            elif snap.get("err"):
                self._say(f"  ошибка: {snap['err']}\n", "err")

    # ── отправка ──
    def _send(self):
        prompt = self.input.get("1.0", tk.END).strip()
        if not prompt:
            return
        self.input.delete("1.0", tk.END)
        self._say(f"\nВы: {prompt}\n", "me")
        body = {"session_id": self.session_id, "task_class": self.cls.get(), "prompt": prompt}
        if self.tier.get() != "авто":
            body["tier"] = self.tier.get()
        self.status.configure(text="думаю…")
        self._bg(lambda: api("POST", "/v1/chat", body), self._answered)

    def _answered(self, res, err):
        if err:
            self._say(f"  сбой связи: {err}\n", "err")
            self.status.configure(text="сбой")
            return
        self.session_id = res.get("session_id") or self.session_id
        if res.get("ok"):
            self._say(f"{res.get('text', '')}\n")
            u = res.get("usage") or {}
            self._say(f"  {res.get('model')} · {res.get('tier')} · {res.get('ms')} мс · "
                      f"{u.get('prompt_tokens', 0)}+{u.get('completion_tokens', 0)} tok\n", "meta")
        else:
            self._say(f"  ошибка: {res.get('err', '?')}\n", "err")
        self.status.configure(text="готово")
        self._refresh_sessions()
        self._refresh_status()

    # ── статус-строка: квоты + burn ──
    def _refresh_status(self):
        def load():
            q = api("GET", "/v1/quota")
            b = api("GET", "/v1/burn?days=1")
            return q, b
        def show(res, err):
            if err:
                return
            q, b = res
            parts = []
            for row in q:
                if row.get("remaining") is not None:
                    parts.append(f"{row['channel']}: {row['remaining']}% ({row.get('window') or '?'})")
                elif row.get("ok"):
                    parts.append(f"{row['channel']}: жив")
            for row in b:
                if row.get("calls"):
                    tok = row["prompt_tokens"] + row["completion_tokens"]
                    parts.append(f"{row['channel']} сегодня: {row['calls']} выз, {tok} tok")
            self.status.configure(text="  ·  ".join(parts) or "телеметрии пока нет")
        self._bg(load, show)
        self.after(60_000, self._refresh_status)


if __name__ == "__main__":
    App().mainloop()
