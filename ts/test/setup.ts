import { beforeAll } from 'vitest';

beforeAll(() => {
    global.window = {} as Window & typeof globalThis;
    global.navigator = {} as Navigator;
});
