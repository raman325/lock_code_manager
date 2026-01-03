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
        typescript({ tsconfig: './tsconfig.build.json' }),
        getBabelOutputPlugin({
            presets: [
                [
                    '@babel/preset-env',
                    {
                        // Target modern browsers that support ES6+ natively
                        // This avoids class transpilation that causes variable conflicts
                        targets: { esmodules: true },
                        modules: false,
                        exclude: ['@babel/plugin-transform-dynamic-import']
                    }
                ]
            ]
        }),
        !dev && terser({ format: { comments: false }, maxWorkers: 1, module: true })
    ],
    // Watch all TypeScript sources during development (only applies in watch mode).
    // Note: unlike the `!dev` condition on terser, this config is always present
    // but has no effect unless Rollup is explicitly run with --watch.
    watch: {
        include: 'ts/**'
    }
};
