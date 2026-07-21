const assert = require('node:assert/strict');
const test = require('node:test');

const { resolveBackgroundUrl } = require('./static/js/main.js');

test('resolves a relative background against the configured instance URL', () => {
    assert.equal(
        resolveBackgroundUrl('https://foundry.example/foundry/', '/worlds/example/background.webp'),
        'https://foundry.example/foundry/worlds/example/background.webp'
    );
});

test('preserves an absolute background URL', () => {
    assert.equal(
        resolveBackgroundUrl('https://foundry.example', 'https://cdn.example/worlds/example/background.webp'),
        'https://cdn.example/worlds/example/background.webp'
    );
});
