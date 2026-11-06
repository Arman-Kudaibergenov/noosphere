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

OLD_START = "const Notify={"
OLD_END = 'document.addEventListener("visibilitychange",()=>{ if(!document.hidden) Notify.resetTitle(); });\n'
i = s.index(OLD_START)
j = s.index(OLD_END, i) + len(OLD_END)
old_block = s[i:j]

NEW = r'''const Notify={
  _asked:false, _prevTitle:null, _ctx:null, _armed:false,
  _states:new Map(), _polling:false, _timer:0, _toastTimer:0,
  supported(){return typeof Notification!=="undefined";},
  ask(){
    if(!this.supported()||this._asked) return;
    this._asked=true;
    try{ if(Notification.permission==="default") Notification.requestPermission().catch(()=>{}); }
    catch(e){}
  },
  lastAi(){
    const n=document.querySelectorAll(".msg.ai");
    return n.length?n[n.length-1].textContent.trim().slice(0,150):"";
  },
  /* владелец не смотрит на экран: вкладка в фоне или окно без фокуса */
  away(){
    if(document.hidden||document.visibilityState==="hidden") return true;
    try{ return !document.hasFocus(); }catch(e){ return false; }
  },
  /* звук: AudioContext заводим только на первом жесте (pointerdown/keydown),
     иначе автоплей-политика оставит контекст в suspended и бипа не будет */
  arm(){
    if(this._armed) return;
    this._armed=true;
    try{
      const AC=window.AudioContext||window.webkitAudioContext;
      if(!AC) return;
      this._ctx=new AC();
      if(this._ctx.state==="suspended") this._ctx.resume().catch(()=>{});
    }catch(e){ this._ctx=null; }
  },
  beep(){
    const c=this._ctx; if(!c) return;
    try{
      if(c.state==="suspended") c.resume().catch(()=>{});
      const t=c.currentTime;
      const o=c.createOscillator(), g=c.createGain();
      o.type="sine"; o.frequency.setValueAtTime(880,t);
      g.gain.setValueAtTime(0.0001,t);                       // тихий: пик 0.05
      g.gain.exponentialRampToValueAtTime(0.05,t+0.012);
      g.gain.exponentialRampToValueAtTime(0.0001,t+0.12);    // 0.12 с
      o.connect(g); g.connect(c.destination);
      o.start(t); o.stop(t+0.13);
    }catch(e){}
  },
  /* «✓» перед прежним заголовком вкладки; снимается при возврате фокуса */
  markTitle(){
    if(this._prevTitle===null) this._prevTitle=document.title||"Ноосфера";
    try{ document.title="✓ "+this._prevTitle; }catch(e){}
  },
  resetTitle(){
    if(this._prevTitle===null) return;
    try{ document.title=this._prevTitle; }catch(e){}
    this._prevTitle=null;
  },
  /* показ через service worker: на Android Chrome конструктор Notification
     бросает исключение, работает только registration.showNotification */
  async show(title,body){
    try{
      if(navigator.serviceWorker){
        const reg=await navigator.serviceWorker.getRegistration();
        if(reg&&reg.showNotification){
          await reg.showNotification(title,{body:(body||"").slice(0,150),tag:"noosphere-done"});
          return;
        }
      }
    }catch(e){}
    try{ new Notification(title,{body:(body||"").slice(0,150),tag:"noosphere-done"}); }catch(e){}
  },
  name(r){ const t=((r&&r.title)||"").trim(); return t||("Сессия "+String((r&&r.id)||"").slice(0,8)); },
  /* тост внутри приложения: системное уведомление на http/без разрешения
     не покажется вовсе, а прогон уже закончился */
  toast(text,sid){
    const t=$("ntoast"); if(!t) return;
    t.querySelector(".ntx").textContent=text;
    const open=t.querySelector(".ntopen");
    open.dataset.sid=sid||"";
    open.hidden=!sid;
    t.hidden=false;
    clearTimeout(this._toastTimer);
    this._toastTimer=setTimeout(()=>{ t.hidden=true; },9000);
  },
  /* завершение: busy→ready/error по ЛЮБОЙ сессии, не только по открытой */
  done(title,body,opts){
    opts=opts||{};
    const nm=opts.name||"", sid=opts.sid||"", bad=!!opts.bad;
    this.toast(nm?(nm+" · "+(bad?"ошибка":"завершено")):(bad?"Ошибка прогона":"Прогон завершён"),sid);
    if(!this.away()) return;              // смотрим на экран — хватит тоста
    this.markTitle();
    try{ if(navigator.vibrate) navigator.vibrate([80,60,80]); }catch(e){}
    this.beep();
    if(this.supported()&&Notification.permission==="granted") this.show(title,body);
  },
  /* сравнение состояний всех сессий; true — если набор/состояния изменились */
  observe(rows){
    const next=new Map();
    for(const r of (rows||[])) next.set(r.id,r.state);
    let changed=false;
    if(this._states.size){
      for(const r of (rows||[])){
        const prev=this._states.get(r.id);
        if(prev===undefined){ changed=true; continue; }
        if(prev===r.state) continue;
        changed=true;
        if(prev==="busy"&&(r.state==="ready"||r.state==="error")){
          const bad=r.state==="error", nm=this.name(r);
          this.done(bad?"Прогон: ошибка":"Прогон завершён",
            bad?(nm+" · последний ход упал, ждёт тебя"):(nm+" · готово, можно читать"),
            {name:nm,sid:r.id,bad:bad});
        }
      }
    }
    this._states=next;
    return changed;
  },
  /* фоновый опрос раз в 5 с: Sessions.load() перерисовывается только при
     реальной смене состояния — иначе список дёргался бы каждые 5 секунд */
  async poll(){
    if(this._polling) return;
    this._polling=true;
    try{
      const rows=await api("GET","/v1/sessions");
      if(this.observe(rows)) Sessions.load();
    }catch(e){ /* связь и баннеры — забота основного поллинга */ }
    finally{ this._polling=false; }
  },
  start(){
    if(this._timer) return;
    this._timer=setInterval(()=>{ this.poll(); },5000);
  }
};
window.addEventListener("focus",()=>Notify.resetTitle());
document.addEventListener("visibilitychange",()=>{ if(!document.hidden) Notify.resetTitle(); });
/* звук разблокируется жестом (первый тап/клавиша), тост — по клику на «открыть» */
document.addEventListener("pointerdown",()=>Notify.arm(),{once:true,passive:true});
document.addEventListener("keydown",()=>Notify.arm(),{once:true});
(function(){
  const t=$("ntoast"); if(!t) return;
  t.querySelector(".ntopen").addEventListener("click",()=>{
    const sid=t.querySelector(".ntopen").dataset.sid||"";
    t.hidden=true;
    if(sid) Sessions.open(sid);
  });
  t.querySelector(".ntz").addEventListener("click",()=>{ t.hidden=true; });
})();

/* ================================================================ fold
   «Квоты и расход» и «Приёмка» — <details class="side-block">. На телефоне они
   по умолчанию свёрнуты (иначе их фиксированная высота выжимала #sessions до
   60px, и список чатов было не видно), заголовок остаётся всегда видимым.
   Состояние раскрытия липкое в localStorage — отдельно для телефона и десктопа. */
const Fold={
  mq:(window.matchMedia?window.matchMedia("(max-width:640px)"):null),
  mobile(){ return !!(this.mq&&this.mq.matches); },
  key(id){ return "fold-"+id+(this.mobile()?"-m":"-d"); },
  get(id){
    try{ const v=localStorage.getItem(this.key(id)); return v===null?null:v==="1"; }
    catch(e){ return null; }
  },
  set(id,open){
    try{ localStorage.setItem(this.key(id),open?"1":"0"); }catch(e){}
  },
  init(){
    const blocks=[].slice.call(document.querySelectorAll(".side-block"));
    if(!blocks.length) return;
    const apply=()=>{
      for(const b of blocks){
        const id=b.id||"block";
        const saved=this.get(id);
        const want=saved===null?!this.mobile():saved;   // на телефоне свёрнуто
        b._fold=want;                                    // наша установка — не сохраняем
        b.open=want;
      }
    };
    for(const b of blocks){
      b.addEventListener("toggle",()=>{
        if(b._fold===b.open) return;   // программная установка из apply()
        b._fold=b.open;
        this.set(b.id||"block",b.open);
      });
    }
    apply();
    if(this.mq&&this.mq.addEventListener) this.mq.addEventListener("change",apply);
  }
};
Fold.init();
'''

s = s[:i] + NEW + s[j:]

def rep(old,new,cnt=1):
    global s
    n=s.count(old)
    if n!=cnt:
        print("FAIL count=%d for %r"%(n,old[:80])); sys.exit(1)
    s=s.replace(old,new)

# --- Sessions.load: отдать список сессий модулю уведомлений ---
rep(
"""      const rows=await api("GET","/v1/sessions");
      Conn.ok();
      S.sessRows=rows;
""",
"""      const rows=await api("GET","/v1/sessions");
      Conn.ok();
      S.sessRows=rows;
      Notify.observe(rows);   // busy→ready/error по любой сессии = прогон завершён
""")

io.open(p,"w",encoding="utf-8").write(s)
print("ok, len %d -> %d"%(len(orig),len(s)))
