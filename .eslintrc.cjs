// https://medium.com/weekly-webtips/how-to-sort-imports-like-a-pro-in-typescript-4ee8afd7258a
module.exports = {
    env: {
        node: true
    },
    extends: [
        'eslint:recommended',
        'plugin:@typescript-eslint/recommended',
        'plugin:prettier/recommended',
        'plugin:import/recommended',
        'plugin:import/typescript',
        'rollup',
        'prettier'
    ],
    overrides: [
        {
            files: ['*.ts', '*.tsx'],
            parserOptions: {
                project: ['./tsconfig.json']
            }
        },
        {
            files: ['**/*.test.ts', '**/test/**/*.ts', 'vitest.config.ts'],
            rules: {
                'import/no-extraneous-dependencies': 'off',
                'sort-keys': 'off'
            }
        }
    ],
    parser: '@typescript-eslint/parser',
    parserOptions: {
        ecmaVersion: 2023,
        project: true,
        sourceType: 'module'
    },
    plugins: [
        '@stylistic/eslint-plugin-js',
        '@typescript-eslint',
        'import',
        'prettier',
        'unused-imports'
    ],
    rules: {
        '@typescript-eslint/camelcase': 'off',
        '@typescript-eslint/no-unused-vars': 'off',
        camelcase: 'off',
        'class-methods-use-this': 'off',
        'import/no-unresolved': 'error',
        'import/order': [
            'error',
            {
                alphabetize: {
                    caseInsensitive: true,
                    order: 'asc'
                },
                groups: [
                    'builtin',
                    'external',
                    'internal',
                    ['sibling', 'parent'],
                    'index',
                    'unknown'
                ],
                'newlines-between': 'always'
            }
        ],
        'no-undefined': 'off',
        'sort-imports': [
            'error',
            {
                allowSeparatedGroups: true,
                ignoreCase: false,
                ignoreDeclarationSort: true,
                ignoreMemberSort: false,
                memberSyntaxSortOrder: ['none', 'all', 'multiple', 'single']
            }
        ],
        'unused-imports/no-unused-imports': 'error'
    },
    settings: {
        'import/resolver': {
            typescript: {
                project: './tsconfig.json'
            }
        }
    }
};
