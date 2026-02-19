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
                'import/no-extraneous-dependencies': 'off', // Test deps (vitest, etc.) are devDependencies
                'sort-keys': 'off'                          // Test objects are ordered for readability, not alphabetically
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
        '@typescript-eslint/naming-convention': [
            'error',
            { selector: 'default', format: ['camelCase'] },
            // Variables: allow snake_case for HA API destructuring, UPPER_CASE/PascalCase for constants
            { selector: 'variable', format: ['camelCase', 'snake_case', 'UPPER_CASE', 'PascalCase'], leadingUnderscore: 'allow' },
            // Parameters: allow snake_case (HA API), PascalCase (mixin Base param)
            { selector: 'parameter', format: ['camelCase', 'snake_case', 'PascalCase'], leadingUnderscore: 'allow' },
            // Functions: allow PascalCase for mixin factory functions
            { selector: 'function', format: ['camelCase', 'PascalCase'] },
            // Methods: allow leading underscore (Lit private method convention)
            { selector: 'method', format: ['camelCase'], leadingUnderscore: 'allow' },
            { selector: 'property', format: null },       // HA API uses snake_case properties
            { selector: 'typeLike', format: ['PascalCase'] },
            { selector: 'enumMember', format: ['PascalCase', 'UPPER_CASE'] },
            { selector: 'import', format: null }           // Allow any import naming
        ],
        '@typescript-eslint/no-unused-vars': 'off',     // Handled by unused-imports plugin
        camelcase: 'off',                               // Disabled in favor of @typescript-eslint/naming-convention
        'class-methods-use-this': 'off',                // Lit lifecycle methods don't use this
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
        'no-undefined': 'off',                          // Allow undefined (used in HA property patterns)
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
