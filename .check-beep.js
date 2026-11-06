/* бип: проверяем, что с реальным (стабовым) AudioContext код не падает
   и укладывается в 0.12 с при тихой громкости */
const fs = require("fs");
const src = fs.readFileSync("client/web/index.html", "utf8");
const code = src.slice(src.indexOf("const Notify={"), src.indexOf("Fold.init();"));

const calls = [];
class Gain {
  constructor() { this.gain = { setValueAtTime: (v, t) => calls.push(["set", v, t]),
    exponentialRampToValueAtTime: (v, t) => calls.push(["ramp", v, t]) }; }
  connect() {}
}
class Osc {
  constructor() { this.frequency = { setValueAtTime: (v, t) => calls.push(["freq", v, t]) }; this.type = ""; }
  connect() {}
  start(t) { calls.push(["start", t]); }
  stop(t) { calls.push(["stop", t]); }
}
class AC {
  constructor() { this.state = "suspended"; this.currentTime = 10; this.destination = {}; }
  resume() { this.state = "running"; return Promise.resolve(); }
  createOscillator() { return new Osc(); }
  createGain() { return new Gain(); }
}
global.window = { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, AudioContext: AC };
global.localStorage = { getItem: () => null, setItem() {} };
global.document = { hidden: false, visibilityState: "visible", hasFocus: () => false, title: "t",
  addEventListener() {}, querySelectorAll: () => [] };
global.$ = () => ({ querySelector: () => ({ textContent: "", dataset: {}, hidden: false, addEventListener() {} }),
  addEventListener() {}, hidden: true });
new Function(code + "\n;global.Notify=Notify;")();
const N = global.Notify;
N.arm();
if (!N._ctx) { console.log("FAIL: AudioContext не создан на жесте"); process.exit(1); }
N.beep();
const stop = calls.find(c => c[0] === "stop");
const peak = calls.filter(c => c[0] === "ramp").map(c => c[1]);
console.log("peak gain:", peak[0], "длительность:", stop[1] - 10, "с");
const okLen = Math.abs(stop[1] - 10 - 0.13) < 0.001;
const okQuiet = peak[0] <= 0.06;
console.log(okLen && okQuiet ? "OK: бип тихий (0.05) и короткий (~0.12 с)" : "FAIL: параметры бипа");
process.exit(okLen && okQuiet ? 0 : 1);
