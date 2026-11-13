// صح أو خطأ | روّق — كتبه Gemini، راجعته شيهانة (١٣ نوفمبر ٢٠٢٦). يحتاج تجربة فعلية في Lens Studio.
// @input Component.Image displayImage
// @input Component.Image badgeImage
// @input Component.Text scoreText
// @input Asset.Texture[] questionTextures
// @input Asset.Texture[] answerTextures
// @input bool[] isTrueAnswers
// @input Asset.Texture badgeCorrect
// @input Asset.Texture badgeIncorrect
// @input SceneObject headTracker
// @input bool invertTilt = false
// @input float tiltAngleThreshold = 15.0
// @input float neutralAngleThreshold = 5.0
// @input float resultDuration = 1.5

var currentState = "START";
var indices = [];
var currentStep = 0;
var score = 0;
var thresholdRad = script.tiltAngleThreshold * (Math.PI / 180);
var neutralRad = script.neutralAngleThreshold * (Math.PI / 180);

var delayEvent = script.createEvent("DelayedCallbackEvent");
delayEvent.bind(function () { nextQuestion(); });

function initGame() {
    if (script.questionTextures.length !== script.answerTextures.length ||
        script.questionTextures.length !== script.isTrueAnswers.length) {
        print("خطأ: يجب أن تتطابق أعداد الأسئلة وبطاقات الإجابات والخيارات المنطقية.");
        return;
    }
    for (var i = 0; i < script.questionTextures.length; i++) { indices.push(i); }
    shuffleIndices(indices);
    if (indices.length > 0) {
        showCurrentQuestion();
        script.badgeImage.enabled = false;
        if (script.scoreText) { script.scoreText.enabled = false; }
        currentState = "WAITING_FOR_TILT";
    }
}

function shuffleIndices(array) {
    for (var i = array.length - 1; i > 0; i--) {
        var j = Math.floor(Math.random() * (i + 1));
        var temp = array[i]; array[i] = array[j]; array[j] = temp;
    }
}

function showCurrentQuestion() {
    script.displayImage.mainPass.baseTex = script.questionTextures[indices[currentStep]];
}

function nextQuestion() {
    currentStep++;
    script.badgeImage.enabled = false;
    if (currentStep < indices.length) {
        showCurrentQuestion();
        currentState = "WAITING_FOR_NEUTRAL";
    } else {
        script.displayImage.enabled = false;
        if (script.scoreText) {
            script.scoreText.enabled = true;
            script.scoreText.text = "نتيجتك: " + score + " من " + indices.length;
        }
        currentState = "DONE";
    }
}

var updateEvent = script.createEvent("UpdateEvent");
updateEvent.bind(function () {
    if (!script.headTracker) { return; }
    var rollZ = script.headTracker.getTransform().getWorldRotation().toEulerAngles().z;
    if (currentState === "WAITING_FOR_NEUTRAL") {
        if (Math.abs(rollZ) <= neutralRad) { currentState = "WAITING_FOR_TILT"; }
        return;
    }
    if (currentState === "WAITING_FOR_TILT") {
        var right = rollZ < -thresholdRad;
        var left = rollZ > thresholdRad;
        if (script.invertTilt) { var t = right; right = left; left = t; }
        var userAnswer = right ? true : (left ? false : null);
        if (userAnswer !== null) {
            var idx = indices[currentStep];
            script.displayImage.mainPass.baseTex = script.answerTextures[idx];
            script.badgeImage.enabled = true;
            if (userAnswer === script.isTrueAnswers[idx]) {
                script.badgeImage.mainPass.baseTex = script.badgeCorrect;
                score++;
            } else {
                script.badgeImage.mainPass.baseTex = script.badgeIncorrect;
            }
            currentState = "SHOWING_RESULT";
            delayEvent.reset(script.resultDuration);
        }
    }
});

initGame();
