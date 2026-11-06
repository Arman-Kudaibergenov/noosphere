import io, sys, re
p="client/web/index.html"
s=io.open(p,encoding="utf-8").read()
orig=s

def rep(old,new,cnt=1):
    global s
    n=s.count(old)
    if n!=cnt:
        print("FAIL count=%d for %r"%(n,old[:70])); sys.exit(1)
    s=s.replace(old,new)

# --- 1. attachmenu: выключенный пункт «Вставить из буфера» ---
rep(
".attachmenu button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}\n",
".attachmenu button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}\n"
".attachmenu button:disabled{opacity:.5;cursor:default;color:var(--dim)}\n")

# --- 2. тост «<сессия> · завершено» ---
rep(
"/* баннеры над лентой */\n",
"""/* тост завершения прогона: системное уведомление на телефоне приходит не всегда
   (нет разрешения, http вместо https), поэтому дублируем внутри приложения */
#ntoast{position:fixed;left:50%;transform:translateX(-50%);z-index:45;
  bottom:calc(92px + var(--sa-b,0px));max-width:min(94vw,420px);
  display:flex;align-items:center;gap:10px;padding:10px 12px;
  border-radius:var(--r-ctl);background:var(--ink);color:#fff;
  box-shadow:0 8px 24px rgba(30,42,40,.28);font:600 13px var(--sans)}
#ntoast .ntx{flex:1;min-width:0;overflow-wrap:anywhere}
#ntoast .ntb{flex:none;border-radius:8px;border:1px solid rgba(255,255,255,.3);
  background:rgba(255,255,255,.16);color:#fff;padding:6px 10px;min-height:32px;
  font:700 12px var(--sans);cursor:pointer}
#ntoast .ntb:active{transform:scale(.96)}
#ntoast .ntz{background:none;border-color:transparent;padding:6px 8px;color:#D8E0DA}

/* баннеры над лентой */
""")

# --- 3. мобильный брейкпоинт: список чатов не сжимается ниже 45vh ---
rep(
"""  aside.side{overflow-y:auto;-webkit-overflow-scrolling:touch}
  #sessions{flex:0 0 auto;overflow:visible;min-height:0}
  .side-block{margin:8px 12px 0}
""",
"""  aside.side{overflow-y:auto;-webkit-overflow-scrolling:touch}
  #sessions{flex:1 0 auto;overflow:visible;min-height:45vh}
  .side-block{margin:8px 12px 0}
  /* заголовок блока — всегда виден и остаётся тач-таргетом ≥40px */
  .side-block>summary{min-height:40px}
""")

io.open(p,"w",encoding="utf-8").write(s)
print("ok, len %d -> %d"%(len(orig),len(s)))
