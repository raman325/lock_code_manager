import { getBabelOutputPlugin } from '@rollup/plugin-babel';
import { nodeResolve } from '@rollup/plugin-node-resolve';
import terser from '@rollup/plugin-terser';
import typescript from '@rollup/plugin-typescript';

const dev = process.env.ROLLUP_WATCH;

export default {
    input: 'ts/main.ts',
    output: {
        file: 'custom_components/lock_code_manager/www/lock-code-manager-strategy.js',
        format: 'es'
    },
    plugins: [
        nodeResolve(),
        typescript(),
        getBabelOutputPlugin({
            presets: [
                [
                    '@babel/preset-env',
                    {
                        // Target modern browsers that support ES6+ natively
                        // This avoids class transpilation that causes variable conflicts
                        targets: { esmodules: true }
                    }
                ]
            ]
        }),
        !dev && terser({ format: { comments: false } })
    ]
};
