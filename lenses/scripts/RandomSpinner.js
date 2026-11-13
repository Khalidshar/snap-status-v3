// عداد الثقافة السعودية | روّق — كتبه Gemini، راجعته شيهانة (١٣ نوفمبر ٢٠٢٦). يحتاج تجربة فعلية في Lens Studio.
// @input Component.Image displayImage
// @input Asset.Texture coverTexture
// @input Asset.Texture[] titleTextures
// @input float spinDuration = 3.0
// @input float spinSpeed = 0.05

var isSpinning = false;
var timer = 0;
var nextChangeTime = 0;
var lastShownIndex = -1;

if (script.coverTexture) { script.displayImage.mainPass.baseTex = script.coverTexture; }

var tapEvent = script.createEvent("TapEvent");
tapEvent.bind(function () {
    if (isSpinning) { return; }
    isSpinning = true;
    timer = script.spinDuration;
    nextChangeTime = script.spinSpeed;
});

var updateEvent = script.createEvent("UpdateEvent");
updateEvent.bind(function (eventData) {
    if (!isSpinning || script.titleTextures.length === 0) { return; }
    var dt = eventData.getDeltaTime();
    timer -= dt;
    nextChangeTime -= dt;
    if (nextChangeTime <= 0) {
        lastShownIndex = Math.floor(Math.random() * script.titleTextures.length);
        script.displayImage.mainPass.baseTex = script.titleTextures[lastShownIndex];
        nextChangeTime = script.spinSpeed;
    }
    if (timer <= 0) {
        isSpinning = false;
        var finalIndex = Math.floor(Math.random() * script.titleTextures.length);
        if (finalIndex === lastShownIndex && script.titleTextures.length > 1) {
            finalIndex = (finalIndex + 1) % script.titleTextures.length;
        }
        script.displayImage.mainPass.baseTex = script.titleTextures[finalIndex];
    }
});
