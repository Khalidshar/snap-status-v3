// Deterministic simulation of the original scripts; no Lens Studio or account required.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function harness(file, inputs, random = () => 0) {
  const events = {};
  let angle = 0;
  const image = () => ({enabled: true, mainPass: {}});
  const script = {
    displayImage: image(), badgeImage: image(), scoreText: {enabled: true, text: ''},
    questionTextures: ['q0', 'q1'], answerTextures: ['a0', 'a1'],
    isTrueAnswers: [true, false], badgeCorrect: 'correct', badgeIncorrect: 'incorrect',
    tiltAngleThreshold: 15, neutralAngleThreshold: 5, resultDuration: 1.5,
    headTracker: {getTransform: () => ({getWorldRotation: () => ({
      toEulerAngles: () => ({z: angle})
    })})},
    titleTextures: ['t0', 't1', 't2'], coverTexture: 'cover', spinDuration: 3,
    spinSpeed: 0.05, invertTilt: false,
    createEvent: name => (events[name] = {bind(fn) {this.fn = fn;},
      reset(duration) {this.duration = duration;}}), ...inputs
  };
  const math = Object.create(Math);
  math.random = random;
  const context = vm.createContext({script, Math: math, print: () => {}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../scripts', file), 'utf8'),
    context, {timeout: 1000});
  return {script, context, events, tilt(value) {
    angle = value; events.UpdateEvent.fn({getDeltaTime: () => 0.1});
  }, update(dt) {events.UpdateEvent.fn({getDeltaTime: () => dt});}};
}
function quizHarness(inputs = {}) {
  const events = {}, created = [];
  let rawX = 0.01, dt = 0.1;
  const rect = (left, right, bottom, top) => ({left, right, bottom, top});
  function V3(x, y, z) { Object.assign(this, {x, y, z}); }
  V3.up = () => new V3(0, 1, 0);
  function V4(r, g, b, a) { Object.assign(this, {r, g, b, a}); }
  function object(name, parent = null) {
    const components = {};
    const obj = {name, enabled: true, layer: 0, getParent: () => parent,
      setParent(p) { parent = p; }, getComponent: type => components[type] || null,
      createComponent(type) {
        const comp = type === 'Component.ScreenTransform'
          ? {anchors: rect(-0.8, 0.8, 0.4, 0.9), screenPointToParentPoint: p => p}
          : {text: '', textFill: {}, outlineSettings: {fill: {}},
             backgroundSettings: {fill: {}}, getSceneObject: () => obj};
        components[type] = comp;
        return comp;
      }};
    obj.createComponent('Component.ScreenTransform');
    return obj;
  }
  const region = object('region');
  function image(name) {
    const obj = object(name, region);
    const image = obj.createComponent('Component.Image');
    Object.assign(image, {enabled: true, mainPass: {baseColor: new V4(1, 1, 1, 1)}});
    return image;
  }
  const scoreObj = object('score', region), scoreText = scoreObj.createComponent('Component.Text');
  scoreText.textFill.color = new V4(0.8, 0.7, 0.2, 1);
  const hintRight = image('right').getSceneObject(), hintLeft = image('left').getSceneObject();
  const script = {
    displayImage: image('card'), badgeImage: image('badge'), scoreText,
    questionTextures: Array.from({length: 20}, (_, i) => 'q' + i),
    answerTextures: Array.from({length: 20}, (_, i) => 'a' + i),
    isTrueAnswers: Array.from({length: 20}, (_, i) => i < 10),
    badgeCorrect: 'correct', badgeIncorrect: 'incorrect', invertTilt: false,
    tiltAngleThreshold: 12, neutralAngleThreshold: 5, resultDuration: 1.5,
    hideOnDone: [hintRight, hintLeft], hintRight, hintLeft,
    faceCamera: {worldSpaceToScreenSpace: () => ({x: 0, y: 0})},
    headTracker: {getTransform: () => ({
      getWorldRotation: () => ({multiplyVec3: () => ({x: rawX})}),
      getWorldPosition: () => new V3(0, 0, 0)
    })},
    createEvent(name) {
      const event = {bind(fn) {this.fn = fn;}, reset(seconds) {this.duration = seconds;}};
      (events[name] ||= []).push(event);
      return event;
    }, ...inputs
  };
  const math = Object.create(Math); math.random = () => 0.5;
  const context = vm.createContext({script, Math: math, print: () => {},
    vec3: V3, vec4: V4, Rect: {create: rect}, getDeltaTime: () => dt,
    HorizontalAlignment: {Center: 0}, VerticalAlignment: {Center: 0},
    HorizontalOverflow: {Overflow: 0}, VerticalOverflow: {Overflow: 0},
    global: {scene: {createSceneObject(name) {const obj = object(name); created.push(obj); return obj;}}}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../scripts/TiltQuiz.js'), 'utf8'), context);
  const h = {script, context, events, created,
    update(value, delta = 0.1) { rawX = value; dt = delta; for (const e of events.UpdateEvent) e.fn(); },
    found() {events.FaceFoundEvent[0].fn();},
    lost() {events.FaceLostEvent[0].fn();},
    delay() {events.DelayedCallbackEvent[0].fn();},
    tap() {events.TapEvent[0].fn();},
    calibrate() {this.found(); for (let i = 0; i < 12; i++) this.update(0.01);}
  };
  return h;
}

test('mobile quiz calibrates from up vector and completes all twenty with replay', () => {
  const h = quizHarness();
  h.update(0.5); assert.equal(h.context.score, 0); // no face
  h.calibrate();
  for (let n = 0; n < 20; n++) {
    if (h.context.currentState === 'WAITING_FOR_NEUTRAL') h.update(0.01);
    const idx = h.context.indices[n];
    const tilt = h.script.isTrueAnswers[idx] ? 0.45 : -0.45;
    h.update(tilt);
    assert.equal(h.context.score, n + 1);
    assert.equal(h.script.badgeImage.mainPass.baseTex, 'correct');
    assert.equal(h.events.DelayedCallbackEvent[0].duration, 1.5);
    h.update(tilt); assert.equal(h.context.score, n + 1);
    h.delay();
    if (n < 19) {
      h.update(tilt); assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
    }
  }
  assert.equal(h.context.currentState, 'DONE');
  assert.equal(h.script.scoreText.text, 'نتيجتك: ٢٠ من ٢٠');
  assert.equal(h.script.displayImage.enabled, false);
  assert.ok(h.script.hideOnDone.every(obj => !obj.enabled));
  h.tap();
  assert.equal(h.context.score, 0);
  assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
  assert.equal(h.context.counterText.text, '١ من ٢٠');
  assert.ok(h.script.hideOnDone.every(obj => obj.enabled));
});

test('mobile quiz pauses progression while face is lost and gates its return', () => {
  const h = quizHarness(); h.calibrate();
  h.lost(); h.update(0.5); assert.equal(h.context.score, 0);
  h.found(); assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
  h.update(0.01); h.update(0.45);
  const score = h.context.score;
  h.lost(); h.delay();
  assert.equal(h.context.currentStep, 0);
  assert.equal(h.context.pendingNext, true);
  h.found(); assert.equal(h.context.currentStep, 1);
  h.update(0.45); assert.equal(h.context.score, score);
  assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
});

test('mobile quiz invertTilt flips right-answer mapping', () => {
  const h = quizHarness({invertTilt: true}); h.calibrate();
  const idx = h.context.indices[0];
  h.update(h.script.isTrueAnswers[idx] ? -0.45 : 0.45);
  assert.equal(h.context.score, 1);
});

test('mobile quiz refuses mismatched answer arrays', () => {
  const h = quizHarness({answerTextures: []}); h.calibrate(); h.update(0.45);
  assert.equal(h.context.currentState, 'START');
  assert.equal(h.context.score, 0);
});

test('mobile quiz animates the tilted hint and clamps ear height', () => {
  const h = quizHarness(); h.calibrate(); h.update(0.18);
  assert.ok(h.context.hintR.s > 1);
  assert.ok(h.context.hintL.s < 1);
  assert.ok(h.context.hintY >= -0.55 && h.context.hintY <= 0.22);
});

test('spinner ignores taps while spinning and settles on a valid title', () => {
  const h = harness('RandomSpinner.js');
  assert.equal(h.script.displayImage.mainPass.baseTex, 'cover');
  h.events.TapEvent.fn(); h.update(1);
  const remaining = h.context.timer;
  h.events.TapEvent.fn();
  assert.equal(h.context.timer, remaining);
  h.update(3);
  assert.equal(h.context.isSpinning, false);
  assert.ok(h.script.titleTextures.includes(h.script.displayImage.mainPass.baseTex));
});
test('spinner must not repeat the previous round final title (base-script defect)', () => {
  const h = harness('RandomSpinner.js', {}, () => 0);
  h.events.TapEvent.fn(); h.update(4);
  const previous = h.script.displayImage.mainPass.baseTex;
  h.events.TapEvent.fn(); h.update(4);
  assert.notEqual(h.script.displayImage.mainPass.baseTex, previous);
});
test('spinner with one title ends normally', () => {
  const h = harness('RandomSpinner.js', {titleTextures: ['only']});
  h.events.TapEvent.fn(); h.update(4);
  assert.equal(h.context.isSpinning, false);
  assert.equal(h.script.displayImage.mainPass.baseTex, 'only');
});

