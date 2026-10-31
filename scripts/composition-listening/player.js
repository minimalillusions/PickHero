"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const TASKS = JSON.parse($("task-data").textContent);
  const INITIAL = JSON.parse($("score-data").textContent);
  const PROMPT = JSON.parse($("prompt-data").textContent);
  const PRISTINE_DOCUMENT = document.documentElement.cloneNode(true);
  const SCHEMA = "pickhero-composition-1";
  const OPEN = {E:28, A:33, D:38};
  const RATE = 44100, LEAD = .12, TAIL = 1.05;
  const CHORDS = {
    Em7:[55,59,62,64], Cmaj7:[55,59,60,64], Am7:[55,57,60,64],
    B7:[54,57,59,63], Fmaj7:[53,57,60,64], G6:[55,59,62,64],
    E7:[56,59,62,64], A7:[55,57,61,64], D7:[54,57,60,62],
    E5:[52,59,64], G5:[55,62,67], A5:[57,64,69],
    B5:[59,66,71], C5:[60,67,72], D5:[50,57,62]
  };
  const PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4v16l13-8z"/></svg>';
  const PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h4v16H6zm8 0h4v16h-4z"/></svg>';
  const CACHE = new Map(), PENDING = new Map();
  const STORAGE = "pickhero-listening-v1-" + (INITIAL.author || "empty");
  let storageOK = true, serial = 0, context, master, source;
  let active = null, buffer = null, mode = "band", offset = 0, started = 0, playing = false, loading = false;
  let comparison = null, reveal = false, chosen = {}, order = {}, ratings = {};
  let origin = INITIAL.pieces.length ? validate(INITIAL) : INITIAL;
  const nodes = new Map();

  function hash(text) {
    let value = 2166136261;
    for (let i=0; i<text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 16777619);
    return (value >>> 0).toString(16);
  }
  function require(condition, message) { if (!condition) throw Error(message); }
  function label(value, name, maximum) {
    require(typeof value === "string" && value.trim().length > 0 && value.length <= maximum, name + " fehlt oder ist zu lang.");
    return value.trim();
  }
  function number(value, min, max, name) {
    require(typeof value === "number" && Number.isFinite(value) && value >= min && value <= max, name + " liegt außerhalb des erlaubten Bereichs.");
    return value;
  }
  function validate(data) {
    require(data && data.schema === SCHEMA, "Die Datei hat nicht das erwartete Kompositionsformat.");
    const author = label(data.author, "Urheberangabe", 40);
    require(Array.isArray(data.pieces) && data.pieces.length === 6, "Es werden genau sechs vollständige Stücke benötigt.");
    const seen = new Set();
    const pieces = data.pieces.map(piece => {
      require(piece && typeof piece === "object", "Ungültiges Stück.");
      const task = TASKS.find(t => t.id === piece.task);
      require(task && !seen.has(task.id), "Eine Aufgabe fehlt, ist doppelt oder unbekannt.");
      seen.add(task.id);
      const title = label(piece.title, "Stücktitel", 60);
      require(Array.isArray(piece.notes) && piece.notes.length >= 12 && piece.notes.length <= 72, title + ": ungültige Notenzahl.");
      let previousEnd = -1;
      const counts = Array(12).fill(0);
      const notes = piece.notes.map(note => {
        require(note && typeof note === "object", title + ": ungültige Note.");
        const beat = number(note.beat, 0, 47.5, "Notenbeginn");
        const duration = number(note.duration, .25, 4, "Notenlänge");
        const velocity = number(note.velocity, .60, .95, "Anschlagstärke");
        require(Number.isInteger(beat * 2), title + ": Noten müssen auf dem Achtelraster beginnen.");
        require(beat + duration <= 48 + 1e-8 && beat >= previousEnd - 1e-8, title + ": überlappende oder zu lange Note.");
        require(Object.hasOwn(OPEN, note.string), title + ": nur E-, A- und D-Saite sind erlaubt.");
        require(Number.isInteger(note.fret) && note.fret >= 0 && note.fret <= 3, title + ": nur Bünde 0 bis 3 sind erlaubt.");
        previousEnd = beat + duration;
        counts[Math.floor(beat / 4)]++;
        return {beat, duration, string:note.string, fret:note.fret, velocity};
      });
      const limit = task.style === "Ruhig" ? 4 : 6;
      require(counts.every(n => n >= 1 && n <= limit), title + ": jeder Takt braucht 1 bis " + limit + " Anschläge.");
      return {task:task.id, title, notes};
    });
    return {schema:SCHEMA, author, pieces};
  }
  function getStored() {
    try {
      const raw = localStorage.getItem(STORAGE);
      if (!raw || raw.length > 400000) return;
      const saved = JSON.parse(raw);
      if (saved.comparison) comparison = validate(saved.comparison);
      if (saved.ratings && typeof saved.ratings === "object" && !Array.isArray(saved.ratings)) ratings = saved.ratings;
      if (saved.order && typeof saved.order === "object") order = saved.order;
    } catch { storageOK = false; }
  }
  function save() {
    try { localStorage.setItem(STORAGE, JSON.stringify({comparison, ratings, order})); }
    catch { storageOK = false; }
    $("storage").textContent = storageOK ? "Deine Bewertungen und Notizen werden nur in diesem Browser gespeichert." : "Dieser Browser speichert hier nicht dauerhaft. Sichere deine Bewertungen über „Bewertung speichern“.";
  }
  function notify(message) { $("notice").textContent = message; }
  function itemFor(task, which) {
    const pack = which === 1 ? comparison : origin;
    const piece = pack?.pieces.find(p => p.task === task.id);
    return piece ? {task, piece, author:pack.author, which} : null;
  }
  function identity(item) { return item.task.id + ":" + hash(JSON.stringify({author:item.author, piece:item.piece})); }
  function choices(task) {
    const items = [itemFor(task, 0), itemFor(task, 1)].filter(Boolean);
    if (items.length === 2 && order[task.id] === 1) items.reverse();
    return items;
  }
  function selected(task) {
    const list = choices(task);
    return list[chosen[task.id] || 0] || list[0];
  }
  function shownTitle(item) {
    if (comparison && origin.pieces.length && !reveal) {
      return "Entwurf " + (choices(item.task).findIndex(x => x.which === item.which) === 0 ? "A" : "B");
    }
    return item.piece.title;
  }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function drawPattern(host, item) {
    host.replaceChildren();
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 480 35");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    svg.classList.add("pattern");
    for (let bar=0; bar<12; bar++) {
      const line = document.createElementNS(svg.namespaceURI, "path");
      line.setAttribute("d", `M${bar*40},0v35`);
      line.setAttribute("stroke", "currentColor"); line.setAttribute("opacity", ".15");
      svg.append(line);
    }
    for (const note of item?.piece.notes || []) {
      const rect = document.createElementNS(svg.namespaceURI, "rect");
      const pitch = OPEN[note.string] + note.fret;
      rect.setAttribute("x", note.beat*10 + 1);
      rect.setAttribute("y", 28-(pitch-28)*1.8);
      rect.setAttribute("width", Math.max(2, note.duration*10-1));
      rect.setAttribute("height", 4); rect.setAttribute("rx", 2);
      rect.setAttribute("fill", "currentColor"); svg.append(rect);
    }
    host.append(svg);
  }
  function buildCards() {
    const parent = $("pieces"); parent.replaceChildren(); nodes.clear();
    for (const [style, caption] of [["Ruhig","Lange Töne. Raum zum Atmen."],["Blues","Eine Frage. Eine freche Antwort."],["Rock","Ein Motiv, das wiederkommt."]]) {
      const group = el("section", "group");
      const head = el("div", "group-label"); head.append(el("b", "", style), el("span", "", caption));
      const cards = el("div", "cards"); group.append(head, cards); parent.append(group);
      for (const task of TASKS.filter(t => t.style === style)) {
        const index = TASKS.indexOf(task), article = el("article", "card");
        article.dataset.style = style; article.dataset.task = task.id;
        const top = el("div", "card-top"), titleArea = el("div", "title-area");
        titleArea.append(el("div", "eyebrow", task.name));
        const title = el("h3"), sub = el("p", "subtitle"); title.id = "title-"+task.id;
        article.setAttribute("aria-labelledby", title.id);
        titleArea.append(title, sub); top.append(titleArea, el("span", "number", String(index+1).padStart(2,"0")));
        const pattern = el("div"), actions = el("div", "card-actions"), play = el("button", "listen", "Anhören");
        play.onclick = () => chooseAndPlay(task);
        const length = el("span", "length", `${time(2880/task.bpm)} · ${task.bpm} BPM`);
        actions.append(play,length);
        const versions = el("div", "versions"); versions.setAttribute("role", "group"); versions.setAttribute("aria-label", "Beiträge für "+task.name);
        const rating = el("div", "rating"), label = el("label", "", "Möchte ich spielen?");
        label.htmlFor = "rating-"+task.id;
        const select = el("select"); select.id = label.htmlFor;
        for (const [value,text] of [["","Noch offen"],["yes","Ja, unbedingt"],["maybe","Vielleicht"],["no","Eher nicht"]]) {
          const option = el("option", "", text); option.value = value; select.append(option);
        }
        const note = el("input", "note-input"); note.type = "text"; note.maxLength = 240;
        note.placeholder = "Dein Eindruck …"; note.setAttribute("aria-label", "Notiz zu "+task.name);
        const storeRating = () => {
          const item = selected(task); if (!item) return;
          ratings[identity(item)] = {choice:select.value, note:note.value.slice(0,240)}; save();
        };
        select.onchange = storeRating; note.oninput = storeRating;
        rating.append(label,select); article.append(top,pattern,actions,versions,rating,note); cards.append(article);
        nodes.set(task.id, {article,title,sub,pattern,play,versions,select,note});
      }
    }
    updateCards();
  }
  function updateCards() {
    for (const task of TASKS) {
      const n = nodes.get(task.id), item = selected(task), list = choices(task);
      n.title.textContent = item ? shownTitle(item) : "Deine Komposition fehlt noch";
      n.sub.textContent = !item ? task.brief : `${task.swing ? "Shuffle" : "Gerade Achtel"} · E / A / D · Bünde 0–3`;
      n.play.disabled = !item; n.select.disabled = !item; n.note.disabled = !item;
      const current = !!active && !!item && identity(active) === identity(item);
      n.play.setAttribute("aria-label", (current && playing ? "Pausieren: " : "Anhören: ")+task.name+(item ? " / "+shownTitle(item) : ""));
      n.article.classList.toggle("active", !!active && !!item && identity(active) === identity(item));
      n.play.textContent = current && playing ? "Pause" : current && offset > 0 ? "Weiterhören" : "Anhören";
      drawPattern(n.pattern,item); n.versions.replaceChildren();
      list.forEach((candidate,i) => {
        if (list.length < 2) return;
        const name = reveal ? candidate.author : "Entwurf "+(i ? "B" : "A");
        const button = el("button", "", name); button.setAttribute("aria-pressed", String((chosen[task.id] || 0) === i));
        button.onclick = () => { chosen[task.id] = i; updateCards(); chooseAndPlay(task); };
        n.versions.append(button);
      });
      const r = item && ratings[identity(item)];
      n.select.value = ["yes","maybe","no"].includes(r?.choice) ? r.choice : "";
      n.note.value = typeof r?.note === "string" ? r.note.slice(0,240) : "";
    }
    $("source-label").textContent = comparison && origin.pieces.length ? "6 Aufgaben · je zwei Beiträge · gleiche Begleitung" : origin.pieces.length ? "6 vollständige Entwürfe · "+origin.author : comparison ? "6 vollständige Entwürfe · "+comparison.author : "Leere Vorlage · Notendaten einsetzen oder als JSON laden";
    $("reveal").hidden = !(comparison && origin.pieces.length);
    $("download-score").disabled = !origin.pieces.length && !comparison;
    $("reveal").textContent = reveal ? "Quellen ausblenden" : "Quellen anzeigen";
    if (active) showActive();
  }
  function time(seconds) { const s=Math.max(0,Math.floor(seconds)); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
  function showActive() {
    $("now-category").textContent = `${String(TASKS.indexOf(active.task)+1).padStart(2,"0")} / ${active.task.style} · ${active.task.bpm} BPM`;
    $("now-title").textContent = shownTitle(active);
    $("now-sub").textContent = active.task.name + ((reveal || !comparison) ? " · "+active.author : "");
  }
  function swingBeat(beat, task) {
    if (!task.swing) return beat;
    const whole=Math.floor(beat), fraction=beat-whole;
    return whole + (fraction <= .5 ? fraction*2*task.swing : task.swing+(fraction-.5)*2*(1-task.swing));
  }
  function sec(beat, task) { return LEAD+swingBeat(beat,task)*60/task.bpm; }
  function frequency(note) { return 440*Math.pow(2,(note-69)/12); }
  function envelope(ctx, target, start, duration, peak, decay=.6) {
    const g=ctx.createGain(); g.connect(target);
    const attack=.006, release=Math.min(.055,duration*.23), end=start+duration;
    g.gain.setValueAtTime(.00001,start);
    g.gain.exponentialRampToValueAtTime(Math.max(.00002,peak),start+attack);
    const sustain=Math.max(.00002,peak*Math.exp(-Math.max(.001,duration-release-attack)*decay));
    g.gain.exponentialRampToValueAtTime(sustain,Math.max(start+attack+.001,end-release));
    g.gain.exponentialRampToValueAtTime(.00001,end);
    return g;
  }
  function sine(ctx,target,hz,start,duration,gain,type="sine") {
    const oscillator=ctx.createOscillator(), level=ctx.createGain();
    oscillator.type=type; oscillator.frequency.value=hz; level.gain.value=gain;
    oscillator.connect(level).connect(target); oscillator.start(start); oscillator.stop(start+duration+.015);
    return oscillator;
  }
  function bass(ctx,target,noise,note,start,duration) {
    const hz=frequency(OPEN[note.string]+note.fret), filter=ctx.createBiquadFilter();
    filter.type="lowpass"; filter.Q.value=.45;
    filter.frequency.setValueAtTime(1900,start);
    filter.frequency.exponentialRampToValueAtTime(500,start+Math.min(.32,duration));
    const g=envelope(ctx,target,start,duration,.33*note.velocity,.65); filter.connect(g);
    [1,.45,.19,.09,.045,.022].forEach((weight,i) => {
      const oscillator=sine(ctx,filter,hz*(i+1),start,duration,weight);
      oscillator.detune.setValueAtTime(4,start); oscillator.detune.exponentialRampToValueAtTime(.1,start+.04);
    });
    const transient=envelope(ctx,filter,start,.022,.04*note.velocity,50);
    const source=ctx.createBufferSource(); source.buffer=noise; source.connect(transient); source.start(start); source.stop(start+.023);
  }
  function keys(ctx,target,pitch,start,duration,strength,pan) {
    const p=ctx.createStereoPanner(); p.pan.value=pan; p.connect(target);
    const g=envelope(ctx,p,start,duration,.035*strength,1.6), hz=frequency(pitch);
    const carrier=sine(ctx,g,hz,start,duration,1);
    const mod=ctx.createOscillator(), amount=ctx.createGain();
    mod.frequency.value=hz*3; amount.gain.setValueAtTime(hz*.7,start);
    amount.gain.exponentialRampToValueAtTime(hz*.015,start+Math.min(1.5,duration));
    mod.connect(amount).connect(carrier.frequency); mod.start(start); mod.stop(start+duration+.015);
    sine(ctx,g,hz*2,start,duration,.12);
  }
  function guitar(ctx,target,pitch,start,duration,strength,pan) {
    const p=ctx.createStereoPanner(); p.pan.value=pan; p.connect(target);
    const g=envelope(ctx,p,start,duration,.028*strength,2.6), hz=frequency(pitch);
    [1,.48,.23,.15,.09,.05].forEach((v,i)=>sine(ctx,g,hz*(i+1),start,duration,v));
  }
  function noiseBuffer(ctx) {
    const b=ctx.createBuffer(1,RATE,RATE), data=b.getChannelData(0);
    let seed=20261024;
    for(let i=0;i<data.length;i++){seed=(Math.imul(1664525,seed)+1013904223)>>>0;data[i]=(seed/4294967296)*2-1;}
    return b;
  }
  function noiseHit(ctx,target,noise,start,length,level,hz,pan=0) {
    const p=ctx.createStereoPanner();p.pan.value=pan;p.connect(target);
    const filter=ctx.createBiquadFilter();filter.type="highpass";filter.frequency.value=hz;
    const g=envelope(ctx,p,start,length,level,30);
    const source=ctx.createBufferSource();source.buffer=noise;source.connect(filter).connect(g);source.start(start,.13);source.stop(start+length+.01);
  }
  function drum(ctx,target,noise,kind,start,strength,soft) {
    if(kind==="kick") {
      const g=envelope(ctx,target,start,.22,.42*strength,18);
      const osc=sine(ctx,g,110,start,.23,1);
      osc.frequency.exponentialRampToValueAtTime(49,start+.07);
    } else if(kind==="snare") {
      if(soft){sine(ctx,envelope(ctx,target,start,.06,.075*strength,35),1650,start,.065,1);noiseHit(ctx,target,noise,start,.07,.035*strength,1500,.12);}
      else {noiseHit(ctx,target,noise,start,.17,.19*strength,1400,.1);sine(ctx,envelope(ctx,target,start,.10,.12*strength,20),185,start,.11,1);}
    } else {
      noiseHit(ctx,target,noise,start,kind==="open"?.22:.065,.042*strength,6500,-.18);
    }
  }
  function backing(ctx,target,noise,task) {
    const q=60/task.bpm, calm=task.style==="Ruhig", rock=task.style==="Rock";
    for(let bar=0;bar<12;bar++) {
      const end=bar===11, base=bar*4, chord=CHORDS[task.chords[bar]];
      const strokes=end?[[0,3.2,.85]]:calm?[[0,2.7,.70],[3,.65,.34]]:rock?[[0,.35,.9],[.5,.25,.48],[2,.4,.75],[3.5,.28,.45]]:[[0,.65,.70],[1.5,.4,.48],[2.5,.5,.56]];
      strokes.forEach(([beat,len,level])=>chord.forEach((pitch,j)=>{
        const at=sec(base+beat,task)+j*.009, duration=len*q;
        (rock?guitar:keys)(ctx,target,pitch,at,duration,level,(j/(chord.length-1)-.5)*.9);
      }));
      const kick=end?[[0,.85]]:calm?[[0,.63],[2,.45]]:rock?[[0,.82],[.5,.44],[2,.70],[2.5,.42]]:[[0,.67],[2,.59]];
      kick.forEach(([beat,v])=>drum(ctx,target,noise,"kick",sec(base+beat,task),v,calm));
      if(!end){
        [1,3].forEach(beat=>drum(ctx,target,noise,"snare",sec(base+beat,task),calm?.55:rock?.8:.58,calm));
        for(let i=0;i<8;i++)drum(ctx,target,noise,(rock&&i===7&&bar%4===3)?"open":"hat",sec(base+i/2,task),(i%2?.38:.66)*(calm?.65:1),calm);
      }
    }
  }
  async function render(item, selectedMode="band") {
    const key=identity(item)+":"+selectedMode;
    if(CACHE.has(key)){const found=CACHE.get(key);CACHE.delete(key);CACHE.set(key,found);return found;}
    if(PENDING.has(key))return PENDING.get(key);
    const work=(async()=>{
      const Offline=window.OfflineAudioContext||window.webkitOfflineAudioContext;
      if(!Offline)throw Error("Dieser Browser unterstützt die Klangerzeugung nicht. Bitte einen aktuellen Browser verwenden.");
      const ctx=new Offline(2,Math.ceil((LEAD+2880/item.task.bpm+TAIL)*RATE),RATE);
      const mix=ctx.createGain();mix.gain.value=.72;mix.connect(ctx.destination);
      const noise=noiseBuffer(ctx);
      if(selectedMode!=="backing")for(const note of item.piece.notes){
        const start=sec(note.beat,item.task),end=sec(note.beat+note.duration,item.task);
        bass(ctx,mix,noise,note,start,end-start);
      }
      if(selectedMode!=="bass")backing(ctx,mix,noise,item.task);
      const output=await ctx.startRendering();
      let peak=0,sum=0,count=0;
      const musicEnd=Math.floor((LEAD+2880/item.task.bpm)*RATE);
      for(let channel=0;channel<output.numberOfChannels;channel++){
        const samples=output.getChannelData(channel);
        for(let i=0;i<samples.length;i++){peak=Math.max(peak,Math.abs(samples[i]));if(i<musicEnd){sum+=samples[i]*samples[i];count++;}}
      }
      // One common reference gain per composition keeps stem switches honest:
      // Bass solo and backing retain their exact level from the full band mix.
      let gain;
      if(selectedMode==="band")gain=Math.min(.84/Math.max(peak,1e-9),.105/Math.max(Math.sqrt(sum/count),1e-9));
      else gain=(await render(item,"band")).gain;
      for(let channel=0;channel<output.numberOfChannels;channel++){
        const samples=output.getChannelData(channel);
        for(let i=0;i<samples.length;i++)samples[i]*=gain;
      }
      const result={buffer:output,gain,peak:peak*gain,rms:Math.sqrt(sum/count)*gain};
      CACHE.set(key,result);while(CACHE.size>4)CACHE.delete(CACHE.keys().next().value);
      return result;
    })();
    PENDING.set(key,work);
    try{return await work;}finally{PENDING.delete(key);}
  }
  function ensureAudio() {
    if(!context || context.state === "closed"){
      const Audio=window.AudioContext||window.webkitAudioContext;
      if(!Audio)throw Error("Dieser Browser unterstützt den Audioplayer nicht.");
      context=new Audio();master=context.createGain();master.gain.value=Number($("volume").value);master.connect(context.destination);
    }
    return context.resume();
  }
  function position() { return playing ? Math.min(buffer.duration,offset+context.currentTime-started) : offset; }
  function disconnectSource(){if(source){source.onended=null;try{source.stop();}catch{}source.disconnect();source=null;}}
  function pause(){offset=position();playing=false;disconnectSource();updateTransport();}
  function stop(){serial++;loading=false;playing=false;offset=0;disconnectSource();updateTransport();}
  function start(at=offset){
    disconnectSource();offset=Math.min(Math.max(0,at),Math.max(0,buffer.duration-.001));
    source=context.createBufferSource();source.buffer=buffer;source.connect(master);started=context.currentTime;playing=true;
    source.onended=()=>{playing=false;offset=0;source=null;updateTransport();};source.start(0,offset);updateTransport();
  }
  function updateTransport(){
    $("play").disabled=!active||loading||!buffer;$("stop").disabled=!active;
    $("play").innerHTML=playing?PAUSE:PLAY;$("play").setAttribute("aria-label",playing?"Pausieren":"Wiedergeben");
    $("wav").disabled=!active||loading;$("seek").disabled=!buffer||loading;
    $("audio-status").textContent=loading?"Hörprobe wird vorbereitet …":playing?"Spielt · Leertaste pausiert":buffer?"Bereit zum Anhören.":"Startet erst nach deinem Klick.";
    updateCards();
  }
  async function loadAndPlay(item, at=0, autoplay=true){
    const request=++serial;disconnectSource();playing=false;offset=at;active=item;buffer=null;loading=true;updateTransport();
    try{
      if(autoplay)await ensureAudio();
      const rendered=await render(item,mode);
      if(request!==serial)return;
      buffer=rendered.buffer;loading=false;$("seek").max=buffer.duration;
      $("duration").textContent=time(buffer.duration);offset=Math.min(at,buffer.duration-.001);
      if(autoplay)start(offset);else updateTransport();
    }catch(error){if(request===serial){loading=false;notify("Wiedergabe nicht möglich: "+error.message);updateTransport();}}
  }
  function chooseAndPlay(task){
    const item=selected(task);if(!item)return;notify("");
    if(active && identity(active)===identity(item) && buffer && !loading){$("play").click();return;}
    void loadAndPlay(item);
  }
  $("play").onclick=async()=>{
    try{if(playing)pause();else if(buffer){await ensureAudio();if(offset>=buffer.duration)offset=0;start();}}
    catch(error){notify("Wiedergabe nicht möglich: "+error.message);}
  };
  $("stop").onclick=stop;
  $("seek").oninput=()=>{const at=Number($("seek").value);if(playing)start(at);else offset=at;};
  $("volume").oninput=()=>{if(master)master.gain.setTargetAtTime(Number($("volume").value),context.currentTime,.015);};
  document.querySelectorAll("[data-mode]").forEach(button=>{
    button.onclick=()=>{
      if(mode===button.dataset.mode)return;
      const at=position(),resume=playing||loading;mode=button.dataset.mode;
      document.querySelectorAll("[data-mode]").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));
      if(active)void loadAndPlay(active,at,resume);
    };
  });
  document.addEventListener("keydown",event=>{
    if(event.code==="Space"&&!event.repeat&&!/INPUT|TEXTAREA|SELECT|BUTTON|A|SUMMARY/.test(event.target.tagName)&&!event.target.isContentEditable){event.preventDefault();if(!$("play").disabled)$("play").click();}
  });
  function tick(){
    const now=buffer?position():0;$("seek").value=now;$("elapsed").textContent=time(now);
    $("bar").textContent=active&&buffer?`Takt ${Math.max(1,Math.min(12,Math.floor((now-LEAD)*active.task.bpm/240)+1))} / 12`:"Takt — / 12";
    requestAnimationFrame(tick);
  }
  function wav(audio){
    const channels=audio.numberOfChannels,length=audio.length,raw=new ArrayBuffer(44+length*channels*2),view=new DataView(raw);
    const ascii=(at,s)=>{for(let i=0;i<s.length;i++)view.setUint8(at+i,s.charCodeAt(i));};
    ascii(0,"RIFF");view.setUint32(4,raw.byteLength-8,true);ascii(8,"WAVEfmt ");view.setUint32(16,16,true);
    view.setUint16(20,1,true);view.setUint16(22,channels,true);view.setUint32(24,audio.sampleRate,true);
    view.setUint32(28,audio.sampleRate*channels*2,true);view.setUint16(32,channels*2,true);view.setUint16(34,16,true);
    ascii(36,"data");view.setUint32(40,length*channels*2,true);
    const samples=Array.from({length:channels},(_,i)=>audio.getChannelData(i));let at=44;
    for(let i=0;i<length;i++)for(let c=0;c<channels;c++){view.setInt16(at,Math.round(Math.max(-1,Math.min(1,samples[c][i]))*32767),true);at+=2;}
    return new Blob([raw],{type:"audio/wav"});
  }
  function download(blob,name){
    const url=URL.createObjectURL(blob),a=el("a");a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  function jsonDownload(value,name){download(new Blob([JSON.stringify(value,null,2)+"\n"],{type:"application/json;charset=utf-8"}),name);}
  $("wav").onclick=async()=>{
    const item=active,selectedMode=mode;if(!item)return;$("wav").disabled=true;
    try{download(wav((await render(item,selectedMode)).buffer),"hoerprobe-"+item.task.id+"-"+selectedMode+".wav");notify("WAV-Datei für die gewählte Besetzung erstellt.");}
    catch(error){notify("WAV konnte nicht erstellt werden: "+error.message);}finally{updateTransport();}
  };
  function importPackage(data){
    const checked=validate(data);
    if(origin.pieces.length)require(checked.author!==origin.author,"Bitte einen Beitrag mit einer anderen Urheberangabe laden.");
    stop();active=null;buffer=null;comparison=checked;chosen={};reveal=false;
    const random=new Uint8Array(6);crypto.getRandomValues(random);
    TASKS.forEach((t,i)=>order[t.id]=random[i]%2);save();updateTransport();
    $("now-title").textContent="Vergleich bereit.";$("now-sub").textContent="Wähle pro Aufgabe Entwurf A oder B.";
    notify("Sechs Stücke geladen. "+(origin.pieces.length?"A und B sind je Aufgabe zufällig zugeordnet.":"Alle sechs Hörproben sind bereit."));
  }
  $("import").onclick=()=>$("import-file").click();
  $("import-file").onchange=async event=>{
    const file=event.target.files[0];if(!file)return;
    try{require(file.size<=262144,"Die Datei ist zu groß (höchstens 256 KB).");importPackage(JSON.parse(await file.text()));}
    catch(error){notify("Datei nicht geladen: "+error.message);}finally{event.target.value="";}
  };
  $("reveal").onclick=()=>{reveal=!reveal;updateCards();};
  $("export-ratings").onclick=()=>{
    const evaluations=TASKS.flatMap(task=>choices(task).map((item,index)=>({task:task.id,version:comparison&&origin.pieces.length?(index?"B":"A"):"Solo",author:item.author,title:item.piece.title,score_id:identity(item),...(ratings[identity(item)]||{choice:"",note:""})})));
    jsonDownload({schema:"pickhero-listening-ratings-1",created:new Date().toISOString(),sources_revealed:reveal,evaluations},"hoervergleich-bewertung.json");
    notify("Bewertung exportiert. Die Datei enthält die Zuordnung von A/B zu den Urheberangaben.");
  };
  $("prompt").value=PROMPT;
  $("copy-prompt").onclick=async()=>{
    try{await navigator.clipboard.writeText(PROMPT);$("copy-prompt").textContent="Auftrag kopiert ✓";notify("Auftrag für Claude kopiert.");}
    catch{$("prompt").focus();$("prompt").select();notify("Bitte den markierten Auftrag mit Strg+C kopieren oder als TXT speichern.");}
  };
  $("download-prompt").onclick=()=>download(new Blob([PROMPT],{type:"text/plain;charset=utf-8"}),"claude-auftrag.txt");
  $("download-score").onclick=()=>jsonDownload(origin.pieces.length?origin:comparison||origin,"kompositionen.json");
  $("download-template").onclick=()=>{
    // Build a fresh document, never copy live cards, persisted scores or ratings.
    const clone=PRISTINE_DOCUMENT.cloneNode(true);
    clone.querySelector("#score-data").textContent=JSON.stringify({schema:SCHEMA,author:"Claude",pieces:[]});
    clone.querySelector("#pieces").replaceChildren();clone.querySelector("#notice").textContent="";
    clone.querySelector("#source-label").textContent="Leere Vorlage";
    clone.querySelector("#now-title").textContent="Wähle einen Groove.";
    clone.querySelector("#now-sub").textContent="Bass, Schlagzeug und dezente Akkorde.";
    clone.querySelector("#prompt").value="";clone.querySelector("#prompt").textContent="";
    clone.querySelector("#reveal").hidden=true;
    download(new Blob(["<!doctype html>\n"+clone.outerHTML],{type:"text/html;charset=utf-8"}),"claude-vorlage.html");
  };
  window.addEventListener("pagehide",()=>{stop();if(context)void context.close();});
  getStored();buildCards();save();tick();
  // Explicit hooks make offline score/audio validation possible without a mic,
  // external renderer, or any dependency on PickHero's recognition pipeline.
  window.ListeningRoom={tasks:TASKS,original:origin,validate,render,wav,importPackage,
    get state(){return {playing,loading,mode,offset:position(),active:active?.task.id,comparison:comparison?.author,cacheSize:CACHE.size};}};
})();
