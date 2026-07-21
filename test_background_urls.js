'use strict';

const assert = require('node:assert/strict');

global.document = {
    addEventListener() {}
};

const { resolveBackgroundUrl } = require('./static/js/main.js');

assert.equal(
    resolveBackgroundUrl(
        'https://foundry.example',
        'worlds/the-world/background.webp'
    ),
    'https://foundry.example/worlds/the-world/background.webp',
    'route-relative status backgrounds should resolve against the Foundry instance'
);

assert.equal(
    resolveBackgroundUrl(
        'https://foundry.example/',
        '/worlds/the-world/background.webp'
    ),
    'https://foundry.example/worlds/the-world/background.webp',
    'joining a relative background should not introduce duplicate slashes'
);

assert.equal(
    resolveBackgroundUrl(
        'https://foundry.example',
        'https://cdn.example/background.webp'
    ),
    'https://cdn.example/background.webp',
    'absolute status backgrounds should be rendered without prepending the instance URL'
);
