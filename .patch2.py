import io, sys
p="client/web/index.html"
s=io.open(p,encoding="utf-8").read()
orig=s

def rep(old,new,cnt=1):
    global s
    n=s.count(old)
    if n!=cnt:
        print("FAIL count=%d for %r"%(n,old[:80])); sys.exit(1)
    s=s.replace(old,new)

# --- 4. разметка тоста: после </main>, перед палитрой ---
rep(
"""<div id="palette" role="dialog" aria-modal="true" aria-label="Поиск по ходам">""",
"""<!-- тост завершения прогона: дублирует системное уведомление внутри приложения -->
<div id="ntoast" role="status" aria-live="assertive" hidden>
  <span class="ntx"></span>
  <button class="ntb ntopen" type="button">открыть</button>
  <button class="ntb ntz" type="button" aria-label="Скрыть уведомление">✕</button>
</div>

<div id="palette" role="dialog" aria-modal="true" aria-label="Поиск по ходам">""")

# --- 5. иконка агента: sparkles вместо «молнии»/шестерёнки ---
rep(
"""  agent:'<path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>',""",
"""  /* агент — две «звёздочки» (sparkles): однозначно не настройки. Раньше здесь
     лежала шестерёнка, и кнопка #send в агентском режиме читалась как «настройки» */
  agent:'<path d="M10 3.2l1.7 4.1 4.1 1.7-4.1 1.7L10 14.8l-1.7-4.1L4.2 9l4.1-1.7z"/>'
       +'<path d="M17.8 14.6l.9 2 2 .9-2 .9-.9 2-.9-2-2-.9 2-.9z"/>',""")

io.open(p,"w",encoding="utf-8").write(s)
print("ok, len %d -> %d"%(len(orig),len(s)))
