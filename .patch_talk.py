import io, sys

P = 'client/web/index.html'
s = open(P, encoding='utf-8').read()


def rep(old, new, tag, count=1):
    global s
    if old not in s:
        print('НЕ НАЙДЕНО:', tag)
        sys.exit(1)
    s = s.replace(old, new, count)


# ---------------------------------------------------------------- 1. CSS
rep(""".iconbtn.rec{background:var(--accent);border-color:var(--accent);color:#fff}""",
    """.iconbtn.rec{background:var(--accent);border-color:var(--accent);color:#fff}
/* режим «Разговор» включён, но микрофон пока молчит (ждёт речь или говорит ответ) */
.iconbtn.armed{border-color:var(--accent);background:var(--accent-soft);color:var(--accent-text)}""",
    'css')

# ---------------------------------------------------------------- 2. icon
rep("""  send:'<path d="M4 12h13"/><path d="m11.5 5.5 6.5 6.5-6.5 6.5"/>'""",
    """  send:'<path d="M4 12h13"/><path d="m11.5 5.5 6.5 6.5-6.5 6.5"/>',
  /* разговор — реплика с волной голоса: «говорить и слышать», не «записать» */
  talk:'<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.5-.6L3 21l1.7-4.6A8.4 8.4 0 0 1 3 11.5 8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5Z"/>'
      +'<path d="M9 9.5v4M12 8v7M15 10v3"/>'""",
    'icon')

# ---------------------------------------------------------------- 3. button
rep("""        <button id="micBtn" class="iconbtn" type="button" aria-label="Голосовой ввод" aria-pressed="false" title="Голосовой ввод"></button>""",
    """        <button id="micBtn" class="iconbtn" type="button" aria-label="Голосовой ввод" aria-pressed="false" title="Голосовой ввод"></button>
        <button id="talkBtn" class="iconbtn" type="button" aria-label="Голосовой разговор: говорить и слышать ответ" aria-pressed="false" title="Разговор: голосом туда и обратно"></button>""",
    'button')

# ---------------------------------------------------------------- 4. Talk
TALK = r'''
/* ============================================================================
   talk — режим «Разговор» (voice mode): микрофон → /v1/stt → обычная отправка
   промпта → ответ → /v1/tts → голос Армана.
   VAD по AnalyserNode: речь кончилась — ~700 мс тишины, и запись закрывается
   сама, кнопку жать не нужно. Конвейер: пока звучит фраза, следующая уже
   синтезируется — ждём только первый синтез, а не весь текст.
   Barge-in: заговорил во время ответа — звук глохнет, начинается новая запись.
   TTS молчит — фраза пропускается тихо, текст ответа всё равно в чате: сбой
   озвучки не должен превращать разговор в ошибку.
============================================================================ */
const Talk={
  on:false,state:"off",            // off | idle | rec | think | speak
  stream:null,ctx:null,src:null,analyser:null,proc:null,sink:null,tdata:null,
  pre:[],chunks:[],floor:0.004,voiceMs:0,silMs:0,bargeMs:0,tRec:0,
  frameMs:128,SIL_MS:700,MIN_VOICE_MS:260,PRE_FRAMES:3,BARGE_MS:420,
  gen:0,audio:null,url:null,fin:null,tStop:0,tFirst:0,chars:0,phrases:0,lastEngine:"",

  async toggle(){ return this.on?this.stop():this.start(); },

  async start(){
    if(this.on) return;
    this._stopVoice();                       // микрофон в окне один
    try{
      this.stream=await navigator.mediaDevices.getUserMedia({audio:{
        channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    }catch(e){
      Feed.msg("err","Микрофон недоступен: "+e.message+" — режим «Разговор» не включён.");
      return;
    }
    const AC=window.AudioContext||window.webkitAudioContext;
    try{ this.ctx=new AC({sampleRate:16000}); }catch(e){ this.ctx=new AC(); }
    this.src=this.ctx.createMediaStreamSource(this.stream);
    this.analyser=this.ctx.createAnalyser();
    this.analyser.fftSize=2048; this.analyser.smoothingTimeConstant=0.4;
    this.tdata=new Float32Array(this.analyser.fftSize);
    this.proc=this.ctx.createScriptProcessor(2048,1,1);
    this.proc.onaudioprocess=e=>this._onAudio(e);
    /* микрофон в колонки не гоним (эхо), но цепочка обязана дойти до destination:
       иначе ScriptProcessor не тикает и VAD молчит */
    this.sink=this.ctx.createGain(); this.sink.gain.value=0;
    this.src.connect(this.analyser); this.analyser.connect(this.proc);
    this.proc.connect(this.sink); this.sink.connect(this.ctx.destination);
    this.frameMs=Math.max(32,Math.round(2048/this.ctx.sampleRate*1000));
    this.on=true; this.gen++; this.state="idle";
    this.pre=[]; this.chunks=[]; this.voiceMs=0; this.silMs=0; this.bargeMs=0;
    this.tStop=0; this.tFirst=0;
    this._paint();
    announce("Режим «Разговор» включён");
    Feed.sys("Разговор: говори в микрофон. Пауза ~0.7 с — отправка, ответ читается голосом. "
      +"Кнопка «Разговор» — выключить и заглушить.");
  },

  async stop(){
    const was=this.on;
    this.on=false; this.state="off"; this.gen++;   // gen гасит незавершённый конвейер
    this._stopAudio();
    if(this.proc){ try{ this.proc.onaudioprocess=null; this.proc.disconnect(); }catch(e){} this.proc=null; }
    if(this.analyser){ try{ this.analyser.disconnect(); }catch(e){} this.analyser=null; }
    if(this.src){ try{ this.src.disconnect(); }catch(e){} this.src=null; }
    if(this.sink){ try{ this.sink.disconnect(); }catch(e){} this.sink=null; }
    if(this.ctx){ try{ await this.ctx.close(); }catch(e){} this.ctx=null; }
    if(this.stream){ this.stream.getTracks().forEach(t=>t.stop()); this.stream=null; }
    this.pre=[]; this.chunks=[]; this.tdata=null;
    this.tStop=0; this.tFirst=0;
    this._paint();
    if(was) announce("Режим «Разговор» выключен");
  },

  /* обычный микрофон (#micBtn) гасим молча: две записи в одном окне — конфликт */
  _stopVoice(){
    if(typeof Voice==="undefined"||!Voice.rec) return;
    Voice.rec=false; Voice.chunks=[];
    if(Voice.ctx){ try{ Voice.ctx.close(); }catch(e){} Voice.ctx=null; }
    if(Voice.stream){ try{ Voice.stream.getTracks().forEach(t=>t.stop()); }catch(e){} Voice.stream=null; }
    const b=$("micBtn");
    if(b){ b.classList.remove("rec"); b.setAttribute("aria-pressed","false"); }
  },

  /* ------------------------------ микрофон: VAD + запись ------------------ */
  _level(){
    if(!this.analyser||!this.tdata) return 0;
    this.analyser.getFloatTimeDomainData(this.tdata);
    let sum=0; const d=this.tdata;
    for(let i=0;i<d.length;i++) sum+=d[i]*d[i];
    return Math.sqrt(sum/d.length);
  },
  _onAudio(e){
    if(!this.on) return;
    const rms=this._level();
    const frame=new Float32Array(e.inputBuffer.getChannelData(0));
    const playing=this.state==="speak";
    /* шумовая полка окна: пока играет ответ, не учимся — иначе эхо из колонок
       поднимает порог и перебивание перестаёт срабатывать */
    if(!playing) this.floor=this.floor*0.97+rms*0.03;
    const th=Math.max(playing?0.020:0.006, this.floor*(playing?3.5:2.2));
    const voiced=rms>th;
    if(playing){
      /* barge-in: порог выше (в колонках звучит голос Ноосферы) и нужна
         накопленная длительность, а не один кадр */
      this.bargeMs = voiced ? this.bargeMs+this.frameMs : 0;
      if(this.bargeMs>=this.BARGE_MS) this._barge();
      return;
    }
    if(this.state==="think"){ this.voiceMs=0; return; }   // ответ ещё идёт, микрофон ждёт
    if(this.state==="idle"){
      this.pre.push(frame);
      while(this.pre.length>this.PRE_FRAMES) this.pre.shift();  // предролл: не срезаем начало
      this.voiceMs = voiced ? this.voiceMs+this.frameMs : 0;
      if(this.voiceMs>=this.MIN_VOICE_MS) this._beginRec();
      return;
    }
    this.chunks.push(frame);                                  // state==="rec"
    if(voiced){ this.silMs=0; }
    else { this.silMs+=this.frameMs; if(this.silMs>=this.SIL_MS) this._stopRec(); }
  },
  _beginRec(){
    this.chunks=this.pre.slice(); this.pre=[];
    this.state="rec"; this.voiceMs=0; this.silMs=0;
    this.tRec=performance.now();
    this._paint();
  },
  async _stopRec(){
    if(this.state!=="rec") return;
    this.state="think"; this._paint();
    this.tStop=performance.now(); this.tFirst=0;
    const chunks=this.chunks; this.chunks=[];
    const rate=Math.round((this.ctx&&this.ctx.sampleRate)||16000);
    if(chunks.length*this.frameMs<250){ this._listen(); return; }   // щелчок, не речь
    const wav=Voice.encodeWav(chunks,rate);
    try{
      const d=await Api.raw("POST","/v1/stt",wav,"application/octet-stream");
      if(d.ok&&d.text&&d.text.trim()){ this._submit(d.text.trim()); return; }
      Feed.sys("Разговор: не расслышал — говори ближе к микрофону.");
    }catch(e){
      Feed.sys("Разговор: распознавание не удалось ("+e.message+") — слушаю дальше.");
    }
    this._listen();
  },

  /* ------------------------------ отправка промпта ------------------------ */
  _submit(text){
    this.state="think"; this.chars=text.length; this.phrases=0; this._paint();
    const ta=$("ta");
    if(ta){ ta.value=text; taAuto(); }
    /* тот же путь, что у кнопки отправки: маршрутизация, флаги агента, модель */
    Promise.resolve()
      .then(()=>send(false))
      .then(()=>{ if(this.on&&this.state==="think") this._listen(); })
      .catch(()=>{ if(this.on&&this.state==="think") this._listen(); });
  },

  /* ------------------------------ озвучка ответа -------------------------- */
  hear(text){
    if(!this.on) return;
    const clean=this._clean(text);
    const phrases=this._split(clean);
    if(!phrases.length){ this._listen(); return; }
    this.chars=clean.length; this.phrases=phrases.length;
    this.gen++; const gen=this.gen;
    this.state="speak"; this.bargeMs=0; this.tFirst=0;
    this._paint();
    this._pipeline(phrases,gen);
  },
  async _pipeline(phrases,gen){
    const pending=new Map();
    let next=0;
    /* конвейер: две фразы в полёте — первая играет, вторая синтезируется */
    const kick=()=>{
      while(pending.size<2&&next<phrases.length){ const i=next++; pending.set(i,this._tts(phrases[i])); }
    };
    kick();
    for(let i=0;i<phrases.length;i++){
      if(gen!==this.gen) return;
      const p=pending.get(i); pending.delete(i); kick();
      let blob=null;
      try{ blob=await p; }catch(e){ blob=null; }    // тихий фолбэк: текст уже в чате
      if(gen!==this.gen) return;
      if(!blob) continue;
      await this._play(blob,gen);
    }
    if(gen===this.gen) this._listen();
  },
  async _tts(text){
    const r=await fetch(Api.server+"/v1/tts",{method:"POST",
      headers:{"Authorization":"Bearer "+Api.token,"Content-Type":"application/json"},
      body:JSON.stringify({text:text,voice:"arman",speed:0.95,nfe_step:12})});
    if(!r.ok) throw new Error("tts HTTP "+r.status);
    this.lastEngine=r.headers.get("X-TTS-Engine")||"";
    const b=await r.blob();
    if(!b||b.size<1024) throw new Error("tts: короткий wav");
    return b;
  },
  _play(blob,gen){
    return new Promise(res=>{
      const url=URL.createObjectURL(blob);
      const a=new Audio(url);
      this.audio=a; this.url=url;
      let closed=false;
      const fin=()=>{
        if(closed) return; closed=true;
        if(this.fin===fin) this.fin=null;
        if(this.audio===a) this.audio=null;
        if(this.url===url) this.url=null;
        try{ URL.revokeObjectURL(url); }catch(e){}
        res(gen===this.gen);
      };
      this.fin=fin;
      a.onended=fin; a.onerror=fin;
      const started=()=>{
        if(gen!==this.gen){ fin(); return; }
        if(!this.tFirst){ this.tFirst=performance.now(); this._reportLatency(); }
      };
      const p=a.play();
      if(p&&p.then) p.then(started).catch(()=>fin());
      else started();
    });
  },
  _stopAudio(){
    if(this.audio){ try{ this.audio.pause(); }catch(e){} }
    const fin=this.fin; this.fin=null;
    if(fin){ try{ fin(); }catch(e){} }
    this.audio=null; this.url=null;
  },
  _barge(){
    if(!this.on) return;
    this.gen++;                       // незавершённый конвейер больше ничего не сыграет
    this._stopAudio();
    this.state="rec"; this.chunks=this.pre.slice(); this.pre=[];
    this.voiceMs=0; this.silMs=0; this.bargeMs=0; this.tFirst=0;
    this.tRec=performance.now();
    announce("Перебил — слушаю");
    this._paint();
  },
  _listen(){
    if(!this.on){ this.state="off"; return; }
    if(this.state==="rec") return;
    this.state="idle"; this.voiceMs=0; this.silMs=0; this.bargeMs=0;
    this._paint();
  },
  _reportLatency(){
    if(!this.tStop||!this.tFirst) return;
    const ms=Math.round(this.tFirst-this.tStop);
    this.tFirst=0; this.tStop=0;
    /* «стоп записи → первый звук» — единственная метрика, которая видит весь
       цикл (STT + LLM + первый синтез + старт Audio), а не только сервер */
    console.info("talk: стоп записи → первый звук "+ms+" мс");
    api("POST","/v1/tts/latency",{stop_to_first_ms:ms,chars:this.chars,
      phrases:this.phrases,engine:this.lastEngine||""}).catch(()=>{});
  },

  /* ------------------------------ текст перед озвучкой -------------------- */
  /* код и разметку вслух не читаем: «бэктик-бэктик» в разговоре — мусор */
  _clean(text){
    let s=String(text==null?"":text);
    s=s.replace(/```[\s\S]*?```/g," ");
    s=s.replace(/~~~[\s\S]*?~~~/g," ");
    s=s.replace(/`[^`]*`/g," ");
    s=s.replace(/!\[[^\]]*\]\([^)]*\)/g," ");
    s=s.replace(/\[([^\]]*)\]\([^)]*\)/g,"$1");
    s=s.replace(/https?:\/\/\S+/g,"ссылка");
    s=s.replace(/^\s{0,3}#{1,6}\s*/gm,"");
    s=s.replace(/\*\*|__|~~/g,"");
    s=s.replace(/^\s*[-*+]\s+/gm,"");
    s=s.replace(/^\s*\|.*\|\s*$/gm," ");
    s=s.replace(/[ \t]+/g," ");
    return s.trim();
  },
  /* фразы по . ! ? … и переводу строки; точку в «3.4» и аббревиатурах не режем */
  _split(text){
    const out=[];
    const add=src=>{
      let t=String(src==null?"":src).replace(/\s+/g," ").trim();
      if(!t||!/[0-9A-Za-zА-Яа-яЁё]/.test(t)) return;
      /* длинную тираду дробим по запятым: первая фраза должна зазвучать раньше,
         чем синтезируется вся простыня (потолок сервера — 600 зн) */
      while(t.length>200){
        let cut=t.lastIndexOf(", ",200);
        if(cut<60) cut=t.lastIndexOf(" ",200);
        if(cut<60) cut=200;
        out.push(t.slice(0,cut+1).trim());
        t=t.slice(cut+1).trim();
        if(!t) break;
      }
      if(t) out.push(t);
    };
    const src=String(text==null?"":text).replace(/\r/g,"");
    for(const para of src.split(/\n+/)){
      let buf="";
      for(let i=0;i<para.length;i++){
        const ch=para[i]; buf+=ch;
        if(".!?…".indexOf(ch)<0) continue;
        const nxt=para[i+1]||"";
        if(nxt&&!/\s/.test(nxt)) continue;
        add(buf); buf="";
        while(i+1<para.length&&/\s/.test(para[i+1])) i++;
      }
      if(buf) add(buf);
    }
    return out;
  },

  _paint(){
    const b=$("talkBtn");
    if(!b) return;
    b.classList.toggle("armed",this.on&&this.state!=="rec");
    b.classList.toggle("rec",this.on&&this.state==="rec");
    b.setAttribute("aria-pressed",this.on?"true":"false");
    b.setAttribute("aria-label",this.on
      ?"Выключить разговор и заглушить звук"
      :"Голосовой разговор: говорить и слышать ответ");
    b.title=this.on
      ?"Разговор включён — нажми, чтобы выключить и заглушить"
      :"Разговор: голосом туда и обратно";
    const m=$("micBtn");
    if(m) m.disabled=this.on;      // пока идёт разговор, поштучный ввод не нужен
  }
};
'''
TALK = TALK.replace('\n', '\n')

marker = '''/* ================================================================ fullscreen'''
if marker not in s:
    print('НЕ НАЙДЕН якорь fullscreen')
    sys.exit(1)
s = s.replace(marker, TALK.lstrip('\n') + '\n' + marker, 1)

# ---------------------------------------------------------------- 5. hook в deliver
rep("""        announce("Ответ получен");""",
    """        announce("Ответ получен");
        Talk.hear(r.text||"");   // голосовой режим: ответ уходит в озвучку (если включён)""",
    'deliver-hook')

# ---------------------------------------------------------------- 6. boot
rep("""  $("micBtn").innerHTML=icon("mic");""",
    """  $("micBtn").innerHTML=icon("mic");
  $("talkBtn").innerHTML=icon("talk");""",
    'boot-icon')

rep("""  $("micBtn").addEventListener("click",()=>Voice.toggle());""",
    """  $("micBtn").addEventListener("click",()=>Voice.toggle());
  $("talkBtn").addEventListener("click",()=>Talk.toggle());""",
    'boot-click')

open(P, 'w', encoding='utf-8').write(s)
print('index.html пропатчен')
