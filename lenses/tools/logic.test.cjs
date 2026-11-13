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
test('quiz ignores held tilt until neutral, then shows exact final score and stops', () => {
  const h = harness('TiltQuiz.js');
  assert.equal(h.context.currentState, 'WAITING_FOR_TILT');
  const idx = h.context.indices[0];
  h.tilt(h.script.isTrueAnswers[idx] ? -0.4 : 0.4);
  assert.equal(h.context.score, 1);
  assert.equal(h.context.currentState, 'SHOWING_RESULT');
  assert.equal(h.events.DelayedCallbackEvent.duration, 1.5);
  h.tilt(0.4);
  assert.equal(h.context.score, 1);
  h.events.DelayedCallbackEvent.fn();
  assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
  h.tilt(0.4);
  assert.equal(h.context.currentState, 'WAITING_FOR_NEUTRAL');
  h.tilt(0);
  assert.equal(h.context.currentState, 'WAITING_FOR_TILT');
  const second = h.context.indices[1];
  h.tilt(h.script.isTrueAnswers[second] ? 0.4 : -0.4); // deliberately wrong
  assert.equal(h.script.badgeImage.mainPass.baseTex, 'incorrect');
  h.events.DelayedCallbackEvent.fn();
  assert.equal(h.context.currentState, 'DONE');
  assert.equal(h.script.scoreText.text, 'نتيجتك: 1 من 2');
  assert.equal(h.script.displayImage.enabled, false);
  h.tilt(-0.4);
  assert.equal(h.context.score, 1);
});
test('quiz invertTilt maps the opposite direction to true', () => {
  const h = harness('TiltQuiz.js', {invertTilt: true, questionTextures: ['q'],
    answerTextures: ['a'], isTrueAnswers: [true]});
  h.tilt(0.4);
  assert.equal(h.context.score, 1);
});
test('quiz refuses mismatched arrays without scheduling answers', () => {
  const h = harness('TiltQuiz.js', {answerTextures: []});
  h.tilt(-0.4);
  assert.equal(h.context.currentState, 'START');
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

