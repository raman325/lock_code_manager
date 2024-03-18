// https://medium.com/weekly-webtips/how-to-sort-imports-like-a-pro-in-typescript-4ee8afd7258a
module.exports = {
    parser: "@typescript-eslint/parser",
    parserOptions: {
        ecmaVersion: 2023,
        sourceType: "module",
    },
    env: {
        node: true,
    },
    plugins: [
        "@stylistic/eslint-plugin-js",
        "@typescript-eslint",
        "import",
        "prettier",
        "unused-imports",
    ],
    extends: [
        "eslint:recommended",
        "plugin:@typescript-eslint/recommended",
        "plugin:prettier/recommended",
        "plugin:import/recommended",
        "plugin:import/typescript",
        "rollup",
        "prettier",
    ],
    rules: {
        "@typescript-eslint/camelcase": "off",
        "@typescript-eslint/no-unused-vars": "off",
        camelcase: "off",
        "class-methods-use-this": "off",
        "import/no-unresolved": "error",
        "import/order": [
            "error",
            {
                groups: [
                    "builtin",
                    "external",
                    "internal",
                    ["sibling", "parent"],
                    "index",
                    "unknown",
                ],
                "newlines-between": "always",
                alphabetize: {
                    order: "asc",
                    caseInsensitive: true,
                },
            },
        ],
        "no-undefined": "off",
        "sort-imports": [
            "error",
            {
                ignoreCase: false,
                ignoreDeclarationSort: true,
                ignoreMemberSort: false,
                memberSyntaxSortOrder: ["none", "all", "multiple", "single"],
                allowSeparatedGroups: true,
            },
        ],
        "unused-imports/no-unused-imports": "error",
        "unused-imports/no-unused-vars": [
            "warn",
            {
                "vars": "all",
                "varsIgnorePattern": "^_",
                "args": "after-used",
                "argsIgnorePattern": "^_",
            },
        ],
    },
    overrides: [
        {
            files: ["*.ts", "*.tsx"],
            parserOptions: {
                project: ["./tsconfig.json"],
            },
        },
    ],
    settings: {
        "import/resolver": {
            typescript: {
                project: "./tsconfig.json",
            },
        },
    },
};
