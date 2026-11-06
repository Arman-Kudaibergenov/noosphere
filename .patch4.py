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

# --- attach menu: выключенный пункт буфера в небезопасном контексте ---
rep(
"""  $("attachBtn").addEventListener("click",e=>{
    e.preventDefault();
    const open=attachMenu.hidden;
    attachMenu.hidden=!open;
    $("attachBtn").setAttribute("aria-expanded",open?"true":"false");
    if(open){ const first=attachMenu.querySelector("button"); if(first) first.focus(); }
  });
  attachMenu.addEventListener("click",async e=>{
    const b=e.target.closest("button[data-act]");
    if(!b) return;
    closeAttach();
    if(b.dataset.act==="file"){ $("fileInput").click(); return; }
    await Uploads.fromClipboard();
  });
""",
"""  /* «Вставить из буфера» живёт только в secure context: по http navigator.clipboard.read
     нет вовсе, поэтому пункт показываем, но выключенным — с подсказкой почему */
  const clipItem=attachMenu.querySelector('button[data-act="clip"]');
  function syncClip(){
    if(!clipItem) return;
    const ok=!!(navigator.clipboard&&navigator.clipboard.read);
    clipItem.disabled=!ok;
    clipItem.title=ok?"Вставить картинку из буфера обмена":"нужен https и разрешение на буфер обмена";
  }
  syncClip();
  $("attachBtn").addEventListener("click",e=>{
    e.preventDefault();
    const open=attachMenu.hidden;
    attachMenu.hidden=!open;
    $("attachBtn").setAttribute("aria-expanded",open?"true":"false");
    if(open){ syncClip(); const first=attachMenu.querySelector("button"); if(first) first.focus(); }
  });
  attachMenu.addEventListener("click",async e=>{
    const b=e.target.closest("button[data-act]");
    if(!b) return;
    if(b.disabled) return;
    closeAttach();
    if(b.dataset.act==="file"){ $("fileInput").click(); return; }
    await Uploads.fromClipboard();
  });
""")

# --- единственный добавленный в boot() вызов: фоновый опрос уведомлений ---
rep(
"""  setBusyUI();
  Feed.emptyState();
  await Promise.allSettled([Sessions.load(),Telemetry.load(),Alerts.load(),Acceptance.load()]);
""",
"""  setBusyUI();
  Feed.emptyState();
  Notify.start();   // уведомления о завершении прогона по любой сессии
  await Promise.allSettled([Sessions.load(),Telemetry.load(),Alerts.load(),Acceptance.load()]);
""")

io.open(p,"w",encoding="utf-8").write(s)
print("ok, len %d -> %d"%(len(orig),len(s)))
