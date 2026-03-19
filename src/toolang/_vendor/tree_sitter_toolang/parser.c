#include "tree_sitter/parser.h"

#if defined(__GNUC__) || defined(__clang__)
#pragma GCC diagnostic ignored "-Wmissing-field-initializers"
#endif

#define LANGUAGE_VERSION 14
#define STATE_COUNT 133
#define LARGE_STATE_COUNT 2
#define SYMBOL_COUNT 61
#define ALIAS_COUNT 0
#define TOKEN_COUNT 31
#define EXTERNAL_TOKEN_COUNT 0
#define FIELD_COUNT 21
#define MAX_ALIAS_SEQUENCE_LENGTH 8
#define PRODUCTION_ID_COUNT 29

enum ts_symbol_identifiers {
  aux_sym_newline_token1 = 1,
  sym_comment = 2,
  sym_inline_comment = 3,
  sym_use_keyword = 4,
  sym_thunk_keyword = 5,
  sym_assign_operator = 6,
  sym_remove_operator = 7,
  sym_arrow = 8,
  sym_colon = 9,
  sym_lparen = 10,
  sym_rparen = 11,
  sym_comma = 12,
  sym_question = 13,
  anon_sym_BQUOTE_BQUOTE_BQUOTE = 14,
  anon_sym_skill = 15,
  anon_sym_service = 16,
  anon_sym_prompt = 17,
  anon_sym_psyche = 18,
  anon_sym_struct = 19,
  anon_sym_stash = 20,
  anon_sym_skills = 21,
  anon_sym_services = 22,
  anon_sym_tools = 23,
  anon_sym_thunks = 24,
  sym_model_subject = 25,
  sym_identifier = 26,
  aux_sym_reference_token1 = 27,
  sym_language = 28,
  sym_prompt_text = 29,
  sym_fence_text = 30,
  sym_source_file = 31,
  sym_newline = 32,
  sym_blank_line = 33,
  sym_use_statement = 34,
  sym_declaration = 35,
  sym_declaration_header = 36,
  sym_parameter_list = 37,
  sym_parameter = 38,
  sym_thunk = 39,
  sym_thunk_header = 40,
  sym_thunk_input = 41,
  sym_directive_line = 42,
  sym_collection_directive = 43,
  sym_model_directive = 44,
  sym_directive_values = 45,
  sym_prompt_line = 46,
  sym_fence_body = 47,
  sym_fence_content_line = 48,
  sym_fence_open = 49,
  sym_fence_close = 50,
  sym_cap_kind = 51,
  sym_decl_kind = 52,
  sym_collection_subject = 53,
  sym_reference = 54,
  sym_directive_value = 55,
  aux_sym_source_file_repeat1 = 56,
  aux_sym_parameter_list_repeat1 = 57,
  aux_sym_thunk_repeat1 = 58,
  aux_sym_directive_values_repeat1 = 59,
  aux_sym_fence_body_repeat1 = 60,
};

static const char * const ts_symbol_names[] = {
  [ts_builtin_sym_end] = "end",
  [aux_sym_newline_token1] = "newline_token1",
  [sym_comment] = "comment",
  [sym_inline_comment] = "inline_comment",
  [sym_use_keyword] = "use_keyword",
  [sym_thunk_keyword] = "thunk_keyword",
  [sym_assign_operator] = "assign_operator",
  [sym_remove_operator] = "remove_operator",
  [sym_arrow] = "arrow",
  [sym_colon] = "colon",
  [sym_lparen] = "lparen",
  [sym_rparen] = "rparen",
  [sym_comma] = "comma",
  [sym_question] = "question",
  [anon_sym_BQUOTE_BQUOTE_BQUOTE] = "```",
  [anon_sym_skill] = "skill",
  [anon_sym_service] = "service",
  [anon_sym_prompt] = "prompt",
  [anon_sym_psyche] = "psyche",
  [anon_sym_struct] = "struct",
  [anon_sym_stash] = "stash",
  [anon_sym_skills] = "skills",
  [anon_sym_services] = "services",
  [anon_sym_tools] = "tools",
  [anon_sym_thunks] = "thunks",
  [sym_model_subject] = "model_subject",
  [sym_identifier] = "identifier",
  [aux_sym_reference_token1] = "reference_token1",
  [sym_language] = "language",
  [sym_prompt_text] = "prompt_text",
  [sym_fence_text] = "fence_text",
  [sym_source_file] = "source_file",
  [sym_newline] = "newline",
  [sym_blank_line] = "blank_line",
  [sym_use_statement] = "use_statement",
  [sym_declaration] = "declaration",
  [sym_declaration_header] = "declaration_header",
  [sym_parameter_list] = "parameter_list",
  [sym_parameter] = "parameter",
  [sym_thunk] = "thunk",
  [sym_thunk_header] = "thunk_header",
  [sym_thunk_input] = "thunk_input",
  [sym_directive_line] = "directive_line",
  [sym_collection_directive] = "collection_directive",
  [sym_model_directive] = "model_directive",
  [sym_directive_values] = "directive_values",
  [sym_prompt_line] = "prompt_line",
  [sym_fence_body] = "fence_body",
  [sym_fence_content_line] = "fence_content_line",
  [sym_fence_open] = "fence_open",
  [sym_fence_close] = "fence_close",
  [sym_cap_kind] = "cap_kind",
  [sym_decl_kind] = "decl_kind",
  [sym_collection_subject] = "collection_subject",
  [sym_reference] = "reference",
  [sym_directive_value] = "directive_value",
  [aux_sym_source_file_repeat1] = "source_file_repeat1",
  [aux_sym_parameter_list_repeat1] = "parameter_list_repeat1",
  [aux_sym_thunk_repeat1] = "thunk_repeat1",
  [aux_sym_directive_values_repeat1] = "directive_values_repeat1",
  [aux_sym_fence_body_repeat1] = "fence_body_repeat1",
};

static const TSSymbol ts_symbol_map[] = {
  [ts_builtin_sym_end] = ts_builtin_sym_end,
  [aux_sym_newline_token1] = aux_sym_newline_token1,
  [sym_comment] = sym_comment,
  [sym_inline_comment] = sym_inline_comment,
  [sym_use_keyword] = sym_use_keyword,
  [sym_thunk_keyword] = sym_thunk_keyword,
  [sym_assign_operator] = sym_assign_operator,
  [sym_remove_operator] = sym_remove_operator,
  [sym_arrow] = sym_arrow,
  [sym_colon] = sym_colon,
  [sym_lparen] = sym_lparen,
  [sym_rparen] = sym_rparen,
  [sym_comma] = sym_comma,
  [sym_question] = sym_question,
  [anon_sym_BQUOTE_BQUOTE_BQUOTE] = anon_sym_BQUOTE_BQUOTE_BQUOTE,
  [anon_sym_skill] = anon_sym_skill,
  [anon_sym_service] = anon_sym_service,
  [anon_sym_prompt] = anon_sym_prompt,
  [anon_sym_psyche] = anon_sym_psyche,
  [anon_sym_struct] = anon_sym_struct,
  [anon_sym_stash] = anon_sym_stash,
  [anon_sym_skills] = anon_sym_skills,
  [anon_sym_services] = anon_sym_services,
  [anon_sym_tools] = anon_sym_tools,
  [anon_sym_thunks] = anon_sym_thunks,
  [sym_model_subject] = sym_model_subject,
  [sym_identifier] = sym_identifier,
  [aux_sym_reference_token1] = aux_sym_reference_token1,
  [sym_language] = sym_language,
  [sym_prompt_text] = sym_prompt_text,
  [sym_fence_text] = sym_fence_text,
  [sym_source_file] = sym_source_file,
  [sym_newline] = sym_newline,
  [sym_blank_line] = sym_blank_line,
  [sym_use_statement] = sym_use_statement,
  [sym_declaration] = sym_declaration,
  [sym_declaration_header] = sym_declaration_header,
  [sym_parameter_list] = sym_parameter_list,
  [sym_parameter] = sym_parameter,
  [sym_thunk] = sym_thunk,
  [sym_thunk_header] = sym_thunk_header,
  [sym_thunk_input] = sym_thunk_input,
  [sym_directive_line] = sym_directive_line,
  [sym_collection_directive] = sym_collection_directive,
  [sym_model_directive] = sym_model_directive,
  [sym_directive_values] = sym_directive_values,
  [sym_prompt_line] = sym_prompt_line,
  [sym_fence_body] = sym_fence_body,
  [sym_fence_content_line] = sym_fence_content_line,
  [sym_fence_open] = sym_fence_open,
  [sym_fence_close] = sym_fence_close,
  [sym_cap_kind] = sym_cap_kind,
  [sym_decl_kind] = sym_decl_kind,
  [sym_collection_subject] = sym_collection_subject,
  [sym_reference] = sym_reference,
  [sym_directive_value] = sym_directive_value,
  [aux_sym_source_file_repeat1] = aux_sym_source_file_repeat1,
  [aux_sym_parameter_list_repeat1] = aux_sym_parameter_list_repeat1,
  [aux_sym_thunk_repeat1] = aux_sym_thunk_repeat1,
  [aux_sym_directive_values_repeat1] = aux_sym_directive_values_repeat1,
  [aux_sym_fence_body_repeat1] = aux_sym_fence_body_repeat1,
};

static const TSSymbolMetadata ts_symbol_metadata[] = {
  [ts_builtin_sym_end] = {
    .visible = false,
    .named = true,
  },
  [aux_sym_newline_token1] = {
    .visible = false,
    .named = false,
  },
  [sym_comment] = {
    .visible = true,
    .named = true,
  },
  [sym_inline_comment] = {
    .visible = true,
    .named = true,
  },
  [sym_use_keyword] = {
    .visible = true,
    .named = true,
  },
  [sym_thunk_keyword] = {
    .visible = true,
    .named = true,
  },
  [sym_assign_operator] = {
    .visible = true,
    .named = true,
  },
  [sym_remove_operator] = {
    .visible = true,
    .named = true,
  },
  [sym_arrow] = {
    .visible = true,
    .named = true,
  },
  [sym_colon] = {
    .visible = true,
    .named = true,
  },
  [sym_lparen] = {
    .visible = true,
    .named = true,
  },
  [sym_rparen] = {
    .visible = true,
    .named = true,
  },
  [sym_comma] = {
    .visible = true,
    .named = true,
  },
  [sym_question] = {
    .visible = true,
    .named = true,
  },
  [anon_sym_BQUOTE_BQUOTE_BQUOTE] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_skill] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_service] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_prompt] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_psyche] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_struct] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_stash] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_skills] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_services] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_tools] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_thunks] = {
    .visible = true,
    .named = false,
  },
  [sym_model_subject] = {
    .visible = true,
    .named = true,
  },
  [sym_identifier] = {
    .visible = true,
    .named = true,
  },
  [aux_sym_reference_token1] = {
    .visible = false,
    .named = false,
  },
  [sym_language] = {
    .visible = true,
    .named = true,
  },
  [sym_prompt_text] = {
    .visible = true,
    .named = true,
  },
  [sym_fence_text] = {
    .visible = true,
    .named = true,
  },
  [sym_source_file] = {
    .visible = true,
    .named = true,
  },
  [sym_newline] = {
    .visible = true,
    .named = true,
  },
  [sym_blank_line] = {
    .visible = true,
    .named = true,
  },
  [sym_use_statement] = {
    .visible = true,
    .named = true,
  },
  [sym_declaration] = {
    .visible = true,
    .named = true,
  },
  [sym_declaration_header] = {
    .visible = true,
    .named = true,
  },
  [sym_parameter_list] = {
    .visible = true,
    .named = true,
  },
  [sym_parameter] = {
    .visible = true,
    .named = true,
  },
  [sym_thunk] = {
    .visible = true,
    .named = true,
  },
  [sym_thunk_header] = {
    .visible = true,
    .named = true,
  },
  [sym_thunk_input] = {
    .visible = true,
    .named = true,
  },
  [sym_directive_line] = {
    .visible = true,
    .named = true,
  },
  [sym_collection_directive] = {
    .visible = true,
    .named = true,
  },
  [sym_model_directive] = {
    .visible = true,
    .named = true,
  },
  [sym_directive_values] = {
    .visible = true,
    .named = true,
  },
  [sym_prompt_line] = {
    .visible = true,
    .named = true,
  },
  [sym_fence_body] = {
    .visible = true,
    .named = true,
  },
  [sym_fence_content_line] = {
    .visible = true,
    .named = true,
  },
  [sym_fence_open] = {
    .visible = true,
    .named = true,
  },
  [sym_fence_close] = {
    .visible = true,
    .named = true,
  },
  [sym_cap_kind] = {
    .visible = true,
    .named = true,
  },
  [sym_decl_kind] = {
    .visible = true,
    .named = true,
  },
  [sym_collection_subject] = {
    .visible = true,
    .named = true,
  },
  [sym_reference] = {
    .visible = true,
    .named = true,
  },
  [sym_directive_value] = {
    .visible = true,
    .named = true,
  },
  [aux_sym_source_file_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_parameter_list_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_thunk_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_directive_values_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_fence_body_repeat1] = {
    .visible = false,
    .named = false,
  },
};

enum ts_field_identifiers {
  field_arrow = 1,
  field_body = 2,
  field_close = 3,
  field_colon = 4,
  field_header = 5,
  field_input = 6,
  field_keyword = 7,
  field_kind = 8,
  field_language = 9,
  field_name = 10,
  field_open = 11,
  field_operator = 12,
  field_optional = 13,
  field_output = 14,
  field_parameter = 15,
  field_parameters = 16,
  field_reference = 17,
  field_subject = 18,
  field_text = 19,
  field_value = 20,
  field_values = 21,
};

static const char * const ts_field_names[] = {
  [0] = NULL,
  [field_arrow] = "arrow",
  [field_body] = "body",
  [field_close] = "close",
  [field_colon] = "colon",
  [field_header] = "header",
  [field_input] = "input",
  [field_keyword] = "keyword",
  [field_kind] = "kind",
  [field_language] = "language",
  [field_name] = "name",
  [field_open] = "open",
  [field_operator] = "operator",
  [field_optional] = "optional",
  [field_output] = "output",
  [field_parameter] = "parameter",
  [field_parameters] = "parameters",
  [field_reference] = "reference",
  [field_subject] = "subject",
  [field_text] = "text",
  [field_value] = "value",
  [field_values] = "values",
};

static const TSFieldMapSlice ts_field_map_slices[PRODUCTION_ID_COUNT] = {
  [1] = {.index = 0, .length = 1},
  [2] = {.index = 1, .length = 2},
  [3] = {.index = 3, .length = 2},
  [4] = {.index = 5, .length = 1},
  [5] = {.index = 6, .length = 3},
  [6] = {.index = 9, .length = 2},
  [7] = {.index = 11, .length = 3},
  [8] = {.index = 14, .length = 1},
  [9] = {.index = 15, .length = 3},
  [10] = {.index = 18, .length = 3},
  [11] = {.index = 21, .length = 3},
  [12] = {.index = 24, .length = 1},
  [13] = {.index = 25, .length = 1},
  [14] = {.index = 26, .length = 4},
  [15] = {.index = 30, .length = 4},
  [16] = {.index = 34, .length = 2},
  [17] = {.index = 36, .length = 4},
  [18] = {.index = 40, .length = 2},
  [19] = {.index = 42, .length = 1},
  [20] = {.index = 43, .length = 5},
  [21] = {.index = 48, .length = 5},
  [22] = {.index = 53, .length = 2},
  [23] = {.index = 55, .length = 5},
  [24] = {.index = 60, .length = 2},
  [25] = {.index = 62, .length = 2},
  [26] = {.index = 64, .length = 5},
  [27] = {.index = 69, .length = 6},
  [28] = {.index = 75, .length = 6},
};

static const TSFieldMapEntry ts_field_map_entries[] = {
  [0] =
    {field_header, 0},
  [1] =
    {field_close, 1},
    {field_header, 0},
  [3] =
    {field_colon, 1},
    {field_keyword, 0},
  [5] =
    {field_text, 0},
  [6] =
    {field_body, 1},
    {field_close, 2},
    {field_header, 0},
  [9] =
    {field_operator, 1},
    {field_subject, 0},
  [11] =
    {field_keyword, 0},
    {field_kind, 1},
    {field_reference, 2},
  [14] =
    {field_value, 1},
  [15] =
    {field_colon, 2},
    {field_keyword, 0},
    {field_name, 1},
  [18] =
    {field_colon, 2},
    {field_input, 1},
    {field_keyword, 0},
  [21] =
    {field_operator, 1},
    {field_subject, 0},
    {field_values, 2},
  [24] =
    {field_value, 0},
  [25] =
    {field_name, 0},
  [26] =
    {field_arrow, 1},
    {field_colon, 3},
    {field_keyword, 0},
    {field_output, 2},
  [30] =
    {field_colon, 3},
    {field_input, 2},
    {field_keyword, 0},
    {field_name, 1},
  [34] =
    {field_value, 0},
    {field_value, 1, .inherited = true},
  [36] =
    {field_colon, 2},
    {field_kind, 0},
    {field_name, 1},
    {field_open, 3},
  [40] =
    {field_name, 0},
    {field_optional, 1},
  [42] =
    {field_parameter, 1},
  [43] =
    {field_arrow, 2},
    {field_colon, 4},
    {field_keyword, 0},
    {field_name, 1},
    {field_output, 3},
  [48] =
    {field_arrow, 2},
    {field_colon, 4},
    {field_input, 1},
    {field_keyword, 0},
    {field_output, 3},
  [53] =
    {field_value, 0, .inherited = true},
    {field_value, 1, .inherited = true},
  [55] =
    {field_colon, 2},
    {field_kind, 0},
    {field_language, 4},
    {field_name, 1},
    {field_open, 3},
  [60] =
    {field_parameter, 1},
    {field_parameter, 2, .inherited = true},
  [62] =
    {field_parameter, 0, .inherited = true},
    {field_parameter, 1, .inherited = true},
  [64] =
    {field_colon, 3},
    {field_kind, 0},
    {field_name, 1},
    {field_open, 4},
    {field_parameters, 2},
  [69] =
    {field_arrow, 3},
    {field_colon, 5},
    {field_input, 2},
    {field_keyword, 0},
    {field_name, 1},
    {field_output, 4},
  [75] =
    {field_colon, 3},
    {field_kind, 0},
    {field_language, 5},
    {field_name, 1},
    {field_open, 4},
    {field_parameters, 2},
};

static const TSSymbol ts_alias_sequences[PRODUCTION_ID_COUNT][MAX_ALIAS_SEQUENCE_LENGTH] = {
  [0] = {0},
};

static const uint16_t ts_non_terminal_alias_map[] = {
  0,
};

static const TSStateId ts_primary_state_ids[STATE_COUNT] = {
  [0] = 0,
  [1] = 1,
  [2] = 2,
  [3] = 3,
  [4] = 4,
  [5] = 5,
  [6] = 6,
  [7] = 7,
  [8] = 8,
  [9] = 9,
  [10] = 10,
  [11] = 11,
  [12] = 12,
  [13] = 13,
  [14] = 14,
  [15] = 15,
  [16] = 16,
  [17] = 17,
  [18] = 18,
  [19] = 19,
  [20] = 20,
  [21] = 21,
  [22] = 22,
  [23] = 23,
  [24] = 24,
  [25] = 25,
  [26] = 26,
  [27] = 27,
  [28] = 28,
  [29] = 8,
  [30] = 30,
  [31] = 28,
  [32] = 32,
  [33] = 33,
  [34] = 34,
  [35] = 35,
  [36] = 36,
  [37] = 37,
  [38] = 38,
  [39] = 39,
  [40] = 40,
  [41] = 41,
  [42] = 42,
  [43] = 43,
  [44] = 44,
  [45] = 45,
  [46] = 46,
  [47] = 47,
  [48] = 48,
  [49] = 49,
  [50] = 50,
  [51] = 51,
  [52] = 52,
  [53] = 53,
  [54] = 54,
  [55] = 55,
  [56] = 56,
  [57] = 57,
  [58] = 58,
  [59] = 59,
  [60] = 60,
  [61] = 61,
  [62] = 62,
  [63] = 63,
  [64] = 64,
  [65] = 65,
  [66] = 66,
  [67] = 67,
  [68] = 68,
  [69] = 69,
  [70] = 70,
  [71] = 71,
  [72] = 72,
  [73] = 73,
  [74] = 74,
  [75] = 75,
  [76] = 76,
  [77] = 77,
  [78] = 8,
  [79] = 79,
  [80] = 80,
  [81] = 81,
  [82] = 82,
  [83] = 83,
  [84] = 84,
  [85] = 85,
  [86] = 86,
  [87] = 87,
  [88] = 88,
  [89] = 89,
  [90] = 90,
  [91] = 91,
  [92] = 92,
  [93] = 93,
  [94] = 94,
  [95] = 95,
  [96] = 96,
  [97] = 97,
  [98] = 98,
  [99] = 99,
  [100] = 100,
  [101] = 101,
  [102] = 102,
  [103] = 103,
  [104] = 104,
  [105] = 105,
  [106] = 106,
  [107] = 107,
  [108] = 108,
  [109] = 109,
  [110] = 110,
  [111] = 111,
  [112] = 112,
  [113] = 113,
  [114] = 114,
  [115] = 115,
  [116] = 116,
  [117] = 117,
  [118] = 118,
  [119] = 119,
  [120] = 120,
  [121] = 121,
  [122] = 122,
  [123] = 123,
  [124] = 124,
  [125] = 125,
  [126] = 126,
  [127] = 127,
  [128] = 128,
  [129] = 129,
  [130] = 130,
  [131] = 131,
  [132] = 132,
};

static bool ts_lex(TSLexer *lexer, TSStateId state) {
  START_LEXER();
  eof = lexer->eof(lexer);
  switch (state) {
    case 0:
      if (eof) ADVANCE(60);
      ADVANCE_MAP(
        '\n', 61,
        '\r', 1,
        '#', 63,
        '(', 72,
        ')', 73,
        ',', 74,
        '-', 69,
        ':', 71,
        '=', 68,
        '?', 75,
        '`', 10,
        'm', 40,
        'p', 45,
        's', 17,
        't', 24,
        'u', 47,
      );
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(0);
      END_STATE();
    case 1:
      if (lookahead == '\n') ADVANCE(61);
      END_STATE();
    case 2:
      if (lookahead == '\n') ADVANCE(61);
      if (lookahead == '\r') ADVANCE(1);
      if (lookahead == '#') ADVANCE(63);
      if (lookahead == '=') ADVANCE(67);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(2);
      if (('-' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(91);
      END_STATE();
    case 3:
      if (lookahead == '\n') ADVANCE(61);
      if (lookahead == '\r') ADVANCE(1);
      if (lookahead == '#') ADVANCE(63);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(3);
      if (lookahead == '-' ||
          ('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(92);
      END_STATE();
    case 4:
      if (lookahead == '\n') ADVANCE(61);
      if (lookahead == '\r') ADVANCE(135);
      if (lookahead == '`') ADVANCE(10);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') ADVANCE(136);
      if (lookahead != 0) ADVANCE(139);
      END_STATE();
    case 5:
      if (lookahead == '\n') ADVANCE(62);
      if (lookahead == '\r') ADVANCE(5);
      if (lookahead != 0) ADVANCE(5);
      END_STATE();
    case 6:
      if (lookahead == '(') ADVANCE(72);
      if (lookahead == ':') ADVANCE(71);
      if (lookahead == '=') ADVANCE(8);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(6);
      if (('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(90);
      END_STATE();
    case 7:
      if (lookahead == '-') ADVANCE(69);
      if (lookahead == '=') ADVANCE(67);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(7);
      END_STATE();
    case 8:
      if (lookahead == '>') ADVANCE(70);
      END_STATE();
    case 9:
      if (lookahead == '`') ADVANCE(76);
      END_STATE();
    case 10:
      if (lookahead == '`') ADVANCE(9);
      END_STATE();
    case 11:
      if (lookahead == 'a') ADVANCE(49);
      if (lookahead == 'r') ADVANCE(53);
      END_STATE();
    case 12:
      if (lookahead == 'c') ADVANCE(26);
      END_STATE();
    case 13:
      if (lookahead == 'c') ADVANCE(51);
      END_STATE();
    case 14:
      if (lookahead == 'c') ADVANCE(20);
      END_STATE();
    case 15:
      if (lookahead == 'c') ADVANCE(21);
      END_STATE();
    case 16:
      if (lookahead == 'd') ADVANCE(22);
      END_STATE();
    case 17:
      if (lookahead == 'e') ADVANCE(44);
      if (lookahead == 'k') ADVANCE(28);
      if (lookahead == 't') ADVANCE(11);
      END_STATE();
    case 18:
      if (lookahead == 'e') ADVANCE(64);
      END_STATE();
    case 19:
      if (lookahead == 'e') ADVANCE(82);
      END_STATE();
    case 20:
      if (lookahead == 'e') ADVANCE(80);
      END_STATE();
    case 21:
      if (lookahead == 'e') ADVANCE(79);
      END_STATE();
    case 22:
      if (lookahead == 'e') ADVANCE(33);
      END_STATE();
    case 23:
      if (lookahead == 'e') ADVANCE(46);
      if (lookahead == 'k') ADVANCE(28);
      if (lookahead == 't') ADVANCE(11);
      END_STATE();
    case 24:
      if (lookahead == 'h') ADVANCE(52);
      if (lookahead == 'o') ADVANCE(42);
      END_STATE();
    case 25:
      if (lookahead == 'h') ADVANCE(84);
      END_STATE();
    case 26:
      if (lookahead == 'h') ADVANCE(19);
      END_STATE();
    case 27:
      if (lookahead == 'h') ADVANCE(54);
      END_STATE();
    case 28:
      if (lookahead == 'i') ADVANCE(36);
      END_STATE();
    case 29:
      if (lookahead == 'i') ADVANCE(14);
      END_STATE();
    case 30:
      if (lookahead == 'i') ADVANCE(15);
      END_STATE();
    case 31:
      if (lookahead == 'k') ADVANCE(66);
      END_STATE();
    case 32:
      if (lookahead == 'k') ADVANCE(65);
      END_STATE();
    case 33:
      if (lookahead == 'l') ADVANCE(89);
      END_STATE();
    case 34:
      if (lookahead == 'l') ADVANCE(78);
      END_STATE();
    case 35:
      if (lookahead == 'l') ADVANCE(48);
      END_STATE();
    case 36:
      if (lookahead == 'l') ADVANCE(34);
      END_STATE();
    case 37:
      if (lookahead == 'm') ADVANCE(43);
      END_STATE();
    case 38:
      if (lookahead == 'n') ADVANCE(31);
      END_STATE();
    case 39:
      if (lookahead == 'n') ADVANCE(32);
      END_STATE();
    case 40:
      if (lookahead == 'o') ADVANCE(16);
      END_STATE();
    case 41:
      if (lookahead == 'o') ADVANCE(37);
      END_STATE();
    case 42:
      if (lookahead == 'o') ADVANCE(35);
      END_STATE();
    case 43:
      if (lookahead == 'p') ADVANCE(50);
      END_STATE();
    case 44:
      if (lookahead == 'r') ADVANCE(55);
      END_STATE();
    case 45:
      if (lookahead == 'r') ADVANCE(41);
      if (lookahead == 's') ADVANCE(57);
      END_STATE();
    case 46:
      if (lookahead == 'r') ADVANCE(56);
      END_STATE();
    case 47:
      if (lookahead == 's') ADVANCE(18);
      END_STATE();
    case 48:
      if (lookahead == 's') ADVANCE(87);
      END_STATE();
    case 49:
      if (lookahead == 's') ADVANCE(25);
      END_STATE();
    case 50:
      if (lookahead == 't') ADVANCE(81);
      END_STATE();
    case 51:
      if (lookahead == 't') ADVANCE(83);
      END_STATE();
    case 52:
      if (lookahead == 'u') ADVANCE(38);
      END_STATE();
    case 53:
      if (lookahead == 'u') ADVANCE(13);
      END_STATE();
    case 54:
      if (lookahead == 'u') ADVANCE(39);
      END_STATE();
    case 55:
      if (lookahead == 'v') ADVANCE(29);
      END_STATE();
    case 56:
      if (lookahead == 'v') ADVANCE(30);
      END_STATE();
    case 57:
      if (lookahead == 'y') ADVANCE(12);
      END_STATE();
    case 58:
      if (eof) ADVANCE(60);
      ADVANCE_MAP(
        '\n', 61,
        '\r', 1,
        '#', 5,
        '(', 72,
        ':', 71,
        '=', 8,
        'p', 45,
        's', 23,
        't', 27,
        'u', 47,
      );
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') SKIP(58);
      END_STATE();
    case 59:
      if (eof) ADVANCE(60);
      ADVANCE_MAP(
        '\n', 61,
        '\r', 94,
        '#', 5,
        'm', 118,
        'p', 123,
        's', 104,
        't', 107,
        'u', 125,
        '\t', 93,
        '\f', 93,
        ' ', 93,
      );
      if (lookahead != 0) ADVANCE(134);
      END_STATE();
    case 60:
      ACCEPT_TOKEN(ts_builtin_sym_end);
      END_STATE();
    case 61:
      ACCEPT_TOKEN(aux_sym_newline_token1);
      END_STATE();
    case 62:
      ACCEPT_TOKEN(sym_comment);
      END_STATE();
    case 63:
      ACCEPT_TOKEN(sym_inline_comment);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(63);
      END_STATE();
    case 64:
      ACCEPT_TOKEN(sym_use_keyword);
      END_STATE();
    case 65:
      ACCEPT_TOKEN(sym_thunk_keyword);
      END_STATE();
    case 66:
      ACCEPT_TOKEN(sym_thunk_keyword);
      if (lookahead == 's') ADVANCE(88);
      END_STATE();
    case 67:
      ACCEPT_TOKEN(sym_assign_operator);
      END_STATE();
    case 68:
      ACCEPT_TOKEN(sym_assign_operator);
      if (lookahead == '>') ADVANCE(70);
      END_STATE();
    case 69:
      ACCEPT_TOKEN(sym_remove_operator);
      END_STATE();
    case 70:
      ACCEPT_TOKEN(sym_arrow);
      END_STATE();
    case 71:
      ACCEPT_TOKEN(sym_colon);
      END_STATE();
    case 72:
      ACCEPT_TOKEN(sym_lparen);
      END_STATE();
    case 73:
      ACCEPT_TOKEN(sym_rparen);
      END_STATE();
    case 74:
      ACCEPT_TOKEN(sym_comma);
      END_STATE();
    case 75:
      ACCEPT_TOKEN(sym_question);
      END_STATE();
    case 76:
      ACCEPT_TOKEN(anon_sym_BQUOTE_BQUOTE_BQUOTE);
      END_STATE();
    case 77:
      ACCEPT_TOKEN(anon_sym_BQUOTE_BQUOTE_BQUOTE);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(139);
      END_STATE();
    case 78:
      ACCEPT_TOKEN(anon_sym_skill);
      END_STATE();
    case 79:
      ACCEPT_TOKEN(anon_sym_service);
      END_STATE();
    case 80:
      ACCEPT_TOKEN(anon_sym_service);
      if (lookahead == 's') ADVANCE(86);
      END_STATE();
    case 81:
      ACCEPT_TOKEN(anon_sym_prompt);
      END_STATE();
    case 82:
      ACCEPT_TOKEN(anon_sym_psyche);
      END_STATE();
    case 83:
      ACCEPT_TOKEN(anon_sym_struct);
      END_STATE();
    case 84:
      ACCEPT_TOKEN(anon_sym_stash);
      END_STATE();
    case 85:
      ACCEPT_TOKEN(anon_sym_skills);
      END_STATE();
    case 86:
      ACCEPT_TOKEN(anon_sym_services);
      END_STATE();
    case 87:
      ACCEPT_TOKEN(anon_sym_tools);
      END_STATE();
    case 88:
      ACCEPT_TOKEN(anon_sym_thunks);
      END_STATE();
    case 89:
      ACCEPT_TOKEN(sym_model_subject);
      END_STATE();
    case 90:
      ACCEPT_TOKEN(sym_identifier);
      if (lookahead == '-' ||
          ('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(90);
      END_STATE();
    case 91:
      ACCEPT_TOKEN(aux_sym_reference_token1);
      if (('-' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(91);
      END_STATE();
    case 92:
      ACCEPT_TOKEN(sym_language);
      if (lookahead == '-' ||
          ('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'Z') ||
          lookahead == '_' ||
          ('a' <= lookahead && lookahead <= 'z')) ADVANCE(92);
      END_STATE();
    case 93:
      ACCEPT_TOKEN(sym_prompt_text);
      ADVANCE_MAP(
        '\n', 61,
        '\r', 94,
        '#', 95,
        'm', 118,
        'p', 123,
        's', 104,
        't', 107,
        'u', 125,
        '\t', 93,
        '\f', 93,
        ' ', 93,
      );
      if (lookahead != 0) ADVANCE(134);
      END_STATE();
    case 94:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == '\n') ADVANCE(61);
      if (lookahead != 0) ADVANCE(134);
      END_STATE();
    case 95:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == '\n') ADVANCE(62);
      if (lookahead == '\r') ADVANCE(95);
      if (lookahead != 0) ADVANCE(95);
      END_STATE();
    case 96:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'a') ADVANCE(127);
      if (lookahead == 'r') ADVANCE(131);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 97:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'c') ADVANCE(108);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 98:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'c') ADVANCE(129);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 99:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'c') ADVANCE(103);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 100:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'd') ADVANCE(105);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 101:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'e') ADVANCE(64);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 102:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'e') ADVANCE(82);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 103:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'e') ADVANCE(80);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 104:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'e') ADVANCE(122);
      if (lookahead == 'k') ADVANCE(109);
      if (lookahead == 't') ADVANCE(96);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 105:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'e') ADVANCE(112);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 106:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'h') ADVANCE(84);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 107:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'h') ADVANCE(130);
      if (lookahead == 'o') ADVANCE(120);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 108:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'h') ADVANCE(102);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 109:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'i') ADVANCE(115);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 110:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'i') ADVANCE(99);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 111:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'k') ADVANCE(66);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 112:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'l') ADVANCE(89);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 113:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'l') ADVANCE(124);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 114:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'l') ADVANCE(126);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 115:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'l') ADVANCE(114);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 116:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'm') ADVANCE(121);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 117:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'n') ADVANCE(111);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 118:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'o') ADVANCE(100);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 119:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'o') ADVANCE(116);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 120:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'o') ADVANCE(113);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 121:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'p') ADVANCE(128);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 122:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'r') ADVANCE(132);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 123:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'r') ADVANCE(119);
      if (lookahead == 's') ADVANCE(133);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 124:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 's') ADVANCE(87);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 125:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 's') ADVANCE(101);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 126:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 's') ADVANCE(85);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 127:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 's') ADVANCE(106);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 128:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 't') ADVANCE(81);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 129:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 't') ADVANCE(83);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 130:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'u') ADVANCE(117);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 131:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'u') ADVANCE(98);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 132:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'v') ADVANCE(110);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 133:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead == 'y') ADVANCE(97);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 134:
      ACCEPT_TOKEN(sym_prompt_text);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(134);
      END_STATE();
    case 135:
      ACCEPT_TOKEN(sym_fence_text);
      if (lookahead == '\n') ADVANCE(61);
      if (lookahead != 0) ADVANCE(139);
      END_STATE();
    case 136:
      ACCEPT_TOKEN(sym_fence_text);
      if (lookahead == '\r') ADVANCE(135);
      if (lookahead == '`') ADVANCE(138);
      if (lookahead == '\t' ||
          lookahead == '\f' ||
          lookahead == ' ') ADVANCE(136);
      if (lookahead != 0 &&
          lookahead != '\t' &&
          lookahead != '\n') ADVANCE(139);
      END_STATE();
    case 137:
      ACCEPT_TOKEN(sym_fence_text);
      if (lookahead == '`') ADVANCE(77);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(139);
      END_STATE();
    case 138:
      ACCEPT_TOKEN(sym_fence_text);
      if (lookahead == '`') ADVANCE(137);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(139);
      END_STATE();
    case 139:
      ACCEPT_TOKEN(sym_fence_text);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(139);
      END_STATE();
    default:
      return false;
  }
}

static const TSLexMode ts_lex_modes[STATE_COUNT] = {
  [0] = {.lex_state = 0},
  [1] = {.lex_state = 58},
  [2] = {.lex_state = 59},
  [3] = {.lex_state = 59},
  [4] = {.lex_state = 59},
  [5] = {.lex_state = 58},
  [6] = {.lex_state = 58},
  [7] = {.lex_state = 59},
  [8] = {.lex_state = 59},
  [9] = {.lex_state = 59},
  [10] = {.lex_state = 59},
  [11] = {.lex_state = 59},
  [12] = {.lex_state = 59},
  [13] = {.lex_state = 59},
  [14] = {.lex_state = 59},
  [15] = {.lex_state = 59},
  [16] = {.lex_state = 59},
  [17] = {.lex_state = 59},
  [18] = {.lex_state = 59},
  [19] = {.lex_state = 59},
  [20] = {.lex_state = 59},
  [21] = {.lex_state = 59},
  [22] = {.lex_state = 59},
  [23] = {.lex_state = 59},
  [24] = {.lex_state = 59},
  [25] = {.lex_state = 59},
  [26] = {.lex_state = 59},
  [27] = {.lex_state = 59},
  [28] = {.lex_state = 59},
  [29] = {.lex_state = 58},
  [30] = {.lex_state = 58},
  [31] = {.lex_state = 58},
  [32] = {.lex_state = 58},
  [33] = {.lex_state = 58},
  [34] = {.lex_state = 58},
  [35] = {.lex_state = 58},
  [36] = {.lex_state = 4},
  [37] = {.lex_state = 4},
  [38] = {.lex_state = 4},
  [39] = {.lex_state = 6},
  [40] = {.lex_state = 2},
  [41] = {.lex_state = 2},
  [42] = {.lex_state = 58},
  [43] = {.lex_state = 0},
  [44] = {.lex_state = 0},
  [45] = {.lex_state = 3},
  [46] = {.lex_state = 3},
  [47] = {.lex_state = 0},
  [48] = {.lex_state = 58},
  [49] = {.lex_state = 4},
  [50] = {.lex_state = 0},
  [51] = {.lex_state = 0},
  [52] = {.lex_state = 0},
  [53] = {.lex_state = 0},
  [54] = {.lex_state = 4},
  [55] = {.lex_state = 0},
  [56] = {.lex_state = 0},
  [57] = {.lex_state = 0},
  [58] = {.lex_state = 4},
  [59] = {.lex_state = 0},
  [60] = {.lex_state = 0},
  [61] = {.lex_state = 3},
  [62] = {.lex_state = 0},
  [63] = {.lex_state = 0},
  [64] = {.lex_state = 4},
  [65] = {.lex_state = 4},
  [66] = {.lex_state = 0},
  [67] = {.lex_state = 0},
  [68] = {.lex_state = 4},
  [69] = {.lex_state = 0},
  [70] = {.lex_state = 0},
  [71] = {.lex_state = 0},
  [72] = {.lex_state = 0},
  [73] = {.lex_state = 4},
  [74] = {.lex_state = 0},
  [75] = {.lex_state = 4},
  [76] = {.lex_state = 0},
  [77] = {.lex_state = 4},
  [78] = {.lex_state = 4},
  [79] = {.lex_state = 4},
  [80] = {.lex_state = 0},
  [81] = {.lex_state = 0},
  [82] = {.lex_state = 0},
  [83] = {.lex_state = 0},
  [84] = {.lex_state = 0},
  [85] = {.lex_state = 0},
  [86] = {.lex_state = 7},
  [87] = {.lex_state = 2},
  [88] = {.lex_state = 0},
  [89] = {.lex_state = 0},
  [90] = {.lex_state = 0},
  [91] = {.lex_state = 6},
  [92] = {.lex_state = 6},
  [93] = {.lex_state = 0},
  [94] = {.lex_state = 0},
  [95] = {.lex_state = 58},
  [96] = {.lex_state = 0},
  [97] = {.lex_state = 0},
  [98] = {.lex_state = 58},
  [99] = {.lex_state = 0},
  [100] = {.lex_state = 0},
  [101] = {.lex_state = 0},
  [102] = {.lex_state = 0},
  [103] = {.lex_state = 0},
  [104] = {.lex_state = 0},
  [105] = {.lex_state = 0},
  [106] = {.lex_state = 0},
  [107] = {.lex_state = 0},
  [108] = {.lex_state = 7},
  [109] = {.lex_state = 58},
  [110] = {.lex_state = 0},
  [111] = {.lex_state = 0},
  [112] = {.lex_state = 2},
  [113] = {.lex_state = 0},
  [114] = {.lex_state = 6},
  [115] = {.lex_state = 0},
  [116] = {.lex_state = 6},
  [117] = {.lex_state = 2},
  [118] = {.lex_state = 0},
  [119] = {.lex_state = 0},
  [120] = {.lex_state = 0},
  [121] = {.lex_state = 0},
  [122] = {.lex_state = 0},
  [123] = {.lex_state = 6},
  [124] = {.lex_state = 0},
  [125] = {.lex_state = 0},
  [126] = {.lex_state = 6},
  [127] = {.lex_state = 0},
  [128] = {.lex_state = 6},
  [129] = {.lex_state = 0},
  [130] = {.lex_state = 2},
  [131] = {.lex_state = 6},
  [132] = {.lex_state = 6},
};

static const uint16_t ts_parse_table[LARGE_STATE_COUNT][SYMBOL_COUNT] = {
  [0] = {
    [ts_builtin_sym_end] = ACTIONS(1),
    [aux_sym_newline_token1] = ACTIONS(1),
    [sym_inline_comment] = ACTIONS(1),
    [sym_use_keyword] = ACTIONS(1),
    [sym_thunk_keyword] = ACTIONS(1),
    [sym_assign_operator] = ACTIONS(1),
    [sym_remove_operator] = ACTIONS(1),
    [sym_arrow] = ACTIONS(1),
    [sym_colon] = ACTIONS(1),
    [sym_lparen] = ACTIONS(1),
    [sym_rparen] = ACTIONS(1),
    [sym_comma] = ACTIONS(1),
    [sym_question] = ACTIONS(1),
    [anon_sym_BQUOTE_BQUOTE_BQUOTE] = ACTIONS(1),
    [anon_sym_skill] = ACTIONS(1),
    [anon_sym_service] = ACTIONS(1),
    [anon_sym_prompt] = ACTIONS(1),
    [anon_sym_psyche] = ACTIONS(1),
    [anon_sym_struct] = ACTIONS(1),
    [anon_sym_stash] = ACTIONS(1),
    [anon_sym_services] = ACTIONS(1),
    [anon_sym_tools] = ACTIONS(1),
    [anon_sym_thunks] = ACTIONS(1),
    [sym_model_subject] = ACTIONS(1),
  },
  [1] = {
    [sym_source_file] = STATE(125),
    [sym_newline] = STATE(31),
    [sym_blank_line] = STATE(5),
    [sym_use_statement] = STATE(5),
    [sym_declaration] = STATE(5),
    [sym_declaration_header] = STATE(36),
    [sym_thunk] = STATE(5),
    [sym_thunk_header] = STATE(2),
    [sym_decl_kind] = STATE(116),
    [aux_sym_source_file_repeat1] = STATE(5),
    [ts_builtin_sym_end] = ACTIONS(3),
    [aux_sym_newline_token1] = ACTIONS(5),
    [sym_comment] = ACTIONS(7),
    [sym_use_keyword] = ACTIONS(9),
    [sym_thunk_keyword] = ACTIONS(11),
    [anon_sym_service] = ACTIONS(13),
    [anon_sym_prompt] = ACTIONS(13),
    [anon_sym_psyche] = ACTIONS(13),
    [anon_sym_struct] = ACTIONS(13),
    [anon_sym_stash] = ACTIONS(13),
  },
};

static const uint16_t ts_small_parse_table[] = {
  [0] = 10,
    ACTIONS(15), 1,
      ts_builtin_sym_end,
    ACTIONS(17), 1,
      aux_sym_newline_token1,
    ACTIONS(24), 1,
      sym_model_subject,
    ACTIONS(26), 1,
      sym_prompt_text,
    STATE(28), 1,
      sym_newline,
    STATE(86), 1,
      sym_collection_subject,
    STATE(60), 2,
      sym_collection_directive,
      sym_model_directive,
    ACTIONS(22), 4,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
    STATE(3), 4,
      sym_blank_line,
      sym_directive_line,
      sym_prompt_line,
      aux_sym_thunk_repeat1,
    ACTIONS(20), 8,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [45] = 10,
    ACTIONS(24), 1,
      sym_model_subject,
    ACTIONS(26), 1,
      sym_prompt_text,
    ACTIONS(28), 1,
      ts_builtin_sym_end,
    ACTIONS(30), 1,
      aux_sym_newline_token1,
    STATE(28), 1,
      sym_newline,
    STATE(86), 1,
      sym_collection_subject,
    STATE(60), 2,
      sym_collection_directive,
      sym_model_directive,
    ACTIONS(22), 4,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
    STATE(4), 4,
      sym_blank_line,
      sym_directive_line,
      sym_prompt_line,
      aux_sym_thunk_repeat1,
    ACTIONS(33), 8,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [90] = 10,
    ACTIONS(35), 1,
      ts_builtin_sym_end,
    ACTIONS(37), 1,
      aux_sym_newline_token1,
    ACTIONS(45), 1,
      sym_model_subject,
    ACTIONS(48), 1,
      sym_prompt_text,
    STATE(28), 1,
      sym_newline,
    STATE(86), 1,
      sym_collection_subject,
    STATE(60), 2,
      sym_collection_directive,
      sym_model_directive,
    ACTIONS(42), 4,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
    STATE(4), 4,
      sym_blank_line,
      sym_directive_line,
      sym_prompt_line,
      aux_sym_thunk_repeat1,
    ACTIONS(40), 8,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [135] = 11,
    ACTIONS(5), 1,
      aux_sym_newline_token1,
    ACTIONS(9), 1,
      sym_use_keyword,
    ACTIONS(11), 1,
      sym_thunk_keyword,
    ACTIONS(51), 1,
      ts_builtin_sym_end,
    ACTIONS(53), 1,
      sym_comment,
    STATE(2), 1,
      sym_thunk_header,
    STATE(31), 1,
      sym_newline,
    STATE(36), 1,
      sym_declaration_header,
    STATE(116), 1,
      sym_decl_kind,
    ACTIONS(13), 5,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
    STATE(6), 5,
      sym_blank_line,
      sym_use_statement,
      sym_declaration,
      sym_thunk,
      aux_sym_source_file_repeat1,
  [177] = 11,
    ACTIONS(55), 1,
      ts_builtin_sym_end,
    ACTIONS(57), 1,
      aux_sym_newline_token1,
    ACTIONS(60), 1,
      sym_comment,
    ACTIONS(63), 1,
      sym_use_keyword,
    ACTIONS(66), 1,
      sym_thunk_keyword,
    STATE(2), 1,
      sym_thunk_header,
    STATE(31), 1,
      sym_newline,
    STATE(36), 1,
      sym_declaration_header,
    STATE(116), 1,
      sym_decl_kind,
    ACTIONS(69), 5,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
    STATE(6), 5,
      sym_blank_line,
      sym_use_statement,
      sym_declaration,
      sym_thunk,
      aux_sym_source_file_repeat1,
  [219] = 2,
    ACTIONS(72), 1,
      ts_builtin_sym_end,
    ACTIONS(74), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [240] = 2,
    ACTIONS(76), 1,
      ts_builtin_sym_end,
    ACTIONS(78), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [261] = 2,
    ACTIONS(80), 1,
      ts_builtin_sym_end,
    ACTIONS(82), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [282] = 2,
    ACTIONS(84), 1,
      ts_builtin_sym_end,
    ACTIONS(86), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [303] = 2,
    ACTIONS(88), 1,
      ts_builtin_sym_end,
    ACTIONS(90), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [324] = 2,
    ACTIONS(92), 1,
      ts_builtin_sym_end,
    ACTIONS(94), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [345] = 2,
    ACTIONS(96), 1,
      ts_builtin_sym_end,
    ACTIONS(98), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [366] = 2,
    ACTIONS(100), 1,
      ts_builtin_sym_end,
    ACTIONS(102), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [387] = 2,
    ACTIONS(104), 1,
      ts_builtin_sym_end,
    ACTIONS(106), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [408] = 2,
    ACTIONS(108), 1,
      ts_builtin_sym_end,
    ACTIONS(110), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [429] = 2,
    ACTIONS(112), 1,
      ts_builtin_sym_end,
    ACTIONS(114), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [450] = 2,
    ACTIONS(116), 1,
      ts_builtin_sym_end,
    ACTIONS(118), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [471] = 2,
    ACTIONS(120), 1,
      ts_builtin_sym_end,
    ACTIONS(122), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [492] = 2,
    ACTIONS(124), 1,
      ts_builtin_sym_end,
    ACTIONS(126), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [513] = 2,
    ACTIONS(128), 1,
      ts_builtin_sym_end,
    ACTIONS(130), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [534] = 2,
    ACTIONS(132), 1,
      ts_builtin_sym_end,
    ACTIONS(134), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [555] = 2,
    ACTIONS(136), 1,
      ts_builtin_sym_end,
    ACTIONS(138), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [576] = 2,
    ACTIONS(140), 1,
      ts_builtin_sym_end,
    ACTIONS(142), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [597] = 2,
    ACTIONS(144), 1,
      ts_builtin_sym_end,
    ACTIONS(146), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [618] = 2,
    ACTIONS(148), 1,
      ts_builtin_sym_end,
    ACTIONS(150), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [639] = 2,
    ACTIONS(152), 1,
      ts_builtin_sym_end,
    ACTIONS(154), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [660] = 2,
    ACTIONS(156), 1,
      ts_builtin_sym_end,
    ACTIONS(158), 15,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
      anon_sym_skills,
      anon_sym_services,
      anon_sym_tools,
      anon_sym_thunks,
      sym_model_subject,
      sym_prompt_text,
  [681] = 1,
    ACTIONS(76), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [694] = 1,
    ACTIONS(160), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [707] = 1,
    ACTIONS(156), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [720] = 1,
    ACTIONS(162), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [733] = 1,
    ACTIONS(164), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [746] = 1,
    ACTIONS(166), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [759] = 1,
    ACTIONS(168), 10,
      ts_builtin_sym_end,
      aux_sym_newline_token1,
      sym_comment,
      sym_use_keyword,
      sym_thunk_keyword,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
      anon_sym_struct,
      anon_sym_stash,
  [772] = 7,
    ACTIONS(170), 1,
      aux_sym_newline_token1,
    ACTIONS(172), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    ACTIONS(174), 1,
      sym_fence_text,
    STATE(35), 1,
      sym_fence_close,
    STATE(79), 1,
      sym_newline,
    STATE(111), 1,
      sym_fence_body,
    STATE(38), 2,
      sym_fence_content_line,
      aux_sym_fence_body_repeat1,
  [795] = 5,
    ACTIONS(176), 1,
      aux_sym_newline_token1,
    ACTIONS(179), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    ACTIONS(181), 1,
      sym_fence_text,
    STATE(79), 1,
      sym_newline,
    STATE(37), 2,
      sym_fence_content_line,
      aux_sym_fence_body_repeat1,
  [812] = 5,
    ACTIONS(170), 1,
      aux_sym_newline_token1,
    ACTIONS(174), 1,
      sym_fence_text,
    ACTIONS(184), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    STATE(79), 1,
      sym_newline,
    STATE(37), 2,
      sym_fence_content_line,
      aux_sym_fence_body_repeat1,
  [829] = 5,
    ACTIONS(186), 1,
      sym_arrow,
    ACTIONS(188), 1,
      sym_colon,
    ACTIONS(190), 1,
      sym_lparen,
    ACTIONS(192), 1,
      sym_identifier,
    STATE(95), 1,
      sym_thunk_input,
  [845] = 4,
    ACTIONS(196), 1,
      aux_sym_reference_token1,
    STATE(43), 1,
      sym_directive_value,
    STATE(107), 1,
      sym_directive_values,
    ACTIONS(194), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [859] = 4,
    ACTIONS(196), 1,
      aux_sym_reference_token1,
    STATE(43), 1,
      sym_directive_value,
    STATE(110), 1,
      sym_directive_values,
    ACTIONS(198), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [873] = 2,
    STATE(112), 1,
      sym_cap_kind,
    ACTIONS(200), 4,
      anon_sym_skill,
      anon_sym_service,
      anon_sym_prompt,
      anon_sym_psyche,
  [883] = 3,
    ACTIONS(204), 1,
      sym_comma,
    STATE(44), 1,
      aux_sym_directive_values_repeat1,
    ACTIONS(202), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [894] = 3,
    ACTIONS(204), 1,
      sym_comma,
    STATE(47), 1,
      aux_sym_directive_values_repeat1,
    ACTIONS(206), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [905] = 4,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    ACTIONS(210), 1,
      sym_inline_comment,
    ACTIONS(212), 1,
      sym_language,
    STATE(68), 1,
      sym_newline,
  [918] = 4,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    ACTIONS(214), 1,
      sym_inline_comment,
    ACTIONS(216), 1,
      sym_language,
    STATE(54), 1,
      sym_newline,
  [931] = 3,
    ACTIONS(220), 1,
      sym_comma,
    STATE(47), 1,
      aux_sym_directive_values_repeat1,
    ACTIONS(218), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [942] = 4,
    ACTIONS(190), 1,
      sym_lparen,
    ACTIONS(223), 1,
      sym_arrow,
    ACTIONS(225), 1,
      sym_colon,
    STATE(109), 1,
      sym_thunk_input,
  [955] = 1,
    ACTIONS(227), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [961] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(231), 1,
      sym_inline_comment,
    STATE(24), 1,
      sym_newline,
  [971] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(233), 1,
      sym_inline_comment,
    STATE(14), 1,
      sym_newline,
  [981] = 1,
    ACTIONS(235), 3,
      aux_sym_newline_token1,
      sym_inline_comment,
      sym_comma,
  [987] = 3,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    ACTIONS(237), 1,
      sym_inline_comment,
    STATE(65), 1,
      sym_newline,
  [997] = 1,
    ACTIONS(239), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1003] = 3,
    ACTIONS(241), 1,
      sym_rparen,
    ACTIONS(243), 1,
      sym_comma,
    STATE(66), 1,
      aux_sym_parameter_list_repeat1,
  [1013] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(245), 1,
      sym_inline_comment,
    STATE(10), 1,
      sym_newline,
  [1023] = 3,
    ACTIONS(247), 1,
      sym_colon,
    ACTIONS(249), 1,
      sym_lparen,
    STATE(129), 1,
      sym_parameter_list,
  [1033] = 1,
    ACTIONS(251), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1039] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(253), 1,
      sym_inline_comment,
    STATE(7), 1,
      sym_newline,
  [1049] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(255), 1,
      sym_inline_comment,
    STATE(11), 1,
      sym_newline,
  [1059] = 1,
    ACTIONS(257), 3,
      aux_sym_newline_token1,
      sym_inline_comment,
      sym_language,
  [1065] = 1,
    ACTIONS(259), 3,
      aux_sym_newline_token1,
      sym_inline_comment,
      sym_comma,
  [1071] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(261), 1,
      sym_inline_comment,
    STATE(19), 1,
      sym_newline,
  [1081] = 1,
    ACTIONS(263), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1087] = 1,
    ACTIONS(265), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1093] = 3,
    ACTIONS(267), 1,
      sym_rparen,
    ACTIONS(269), 1,
      sym_comma,
    STATE(66), 1,
      aux_sym_parameter_list_repeat1,
  [1103] = 3,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    ACTIONS(272), 1,
      sym_inline_comment,
    STATE(75), 1,
      sym_newline,
  [1113] = 1,
    ACTIONS(274), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1119] = 2,
    ACTIONS(278), 1,
      sym_question,
    ACTIONS(276), 2,
      sym_rparen,
      sym_comma,
  [1127] = 3,
    ACTIONS(243), 1,
      sym_comma,
    ACTIONS(280), 1,
      sym_rparen,
    STATE(55), 1,
      aux_sym_parameter_list_repeat1,
  [1137] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(282), 1,
      sym_inline_comment,
    STATE(17), 1,
      sym_newline,
  [1147] = 3,
    ACTIONS(5), 1,
      aux_sym_newline_token1,
    ACTIONS(284), 1,
      sym_inline_comment,
    STATE(30), 1,
      sym_newline,
  [1157] = 1,
    ACTIONS(286), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1163] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(288), 1,
      sym_inline_comment,
    STATE(9), 1,
      sym_newline,
  [1173] = 1,
    ACTIONS(290), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1179] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(292), 1,
      sym_inline_comment,
    STATE(22), 1,
      sym_newline,
  [1189] = 1,
    ACTIONS(294), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1195] = 1,
    ACTIONS(78), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1201] = 1,
    ACTIONS(296), 3,
      aux_sym_newline_token1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
      sym_fence_text,
  [1207] = 3,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    ACTIONS(298), 1,
      sym_inline_comment,
    STATE(13), 1,
      sym_newline,
  [1217] = 2,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    STATE(77), 1,
      sym_newline,
  [1224] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(21), 1,
      sym_newline,
  [1231] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(15), 1,
      sym_newline,
  [1238] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(23), 1,
      sym_newline,
  [1245] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(16), 1,
      sym_newline,
  [1252] = 1,
    ACTIONS(300), 2,
      sym_assign_operator,
      sym_remove_operator,
  [1257] = 2,
    ACTIONS(196), 1,
      aux_sym_reference_token1,
    STATE(62), 1,
      sym_directive_value,
  [1264] = 2,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    STATE(64), 1,
      sym_newline,
  [1271] = 2,
    ACTIONS(302), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    STATE(46), 1,
      sym_fence_open,
  [1278] = 1,
    ACTIONS(304), 2,
      sym_rparen,
      sym_comma,
  [1283] = 2,
    ACTIONS(306), 1,
      sym_identifier,
    STATE(101), 1,
      sym_parameter,
  [1290] = 2,
    ACTIONS(306), 1,
      sym_identifier,
    STATE(70), 1,
      sym_parameter,
  [1297] = 2,
    ACTIONS(5), 1,
      aux_sym_newline_token1,
    STATE(32), 1,
      sym_newline,
  [1304] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(25), 1,
      sym_newline,
  [1311] = 2,
    ACTIONS(308), 1,
      sym_arrow,
    ACTIONS(310), 1,
      sym_colon,
  [1318] = 1,
    ACTIONS(312), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [1323] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(26), 1,
      sym_newline,
  [1330] = 1,
    ACTIONS(314), 2,
      sym_arrow,
      sym_colon,
  [1335] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(18), 1,
      sym_newline,
  [1342] = 2,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    STATE(49), 1,
      sym_newline,
  [1349] = 1,
    ACTIONS(316), 2,
      sym_rparen,
      sym_comma,
  [1354] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(12), 1,
      sym_newline,
  [1361] = 2,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    STATE(73), 1,
      sym_newline,
  [1368] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(20), 1,
      sym_newline,
  [1375] = 2,
    ACTIONS(208), 1,
      aux_sym_newline_token1,
    STATE(58), 1,
      sym_newline,
  [1382] = 2,
    ACTIONS(229), 1,
      aux_sym_newline_token1,
    STATE(27), 1,
      sym_newline,
  [1389] = 1,
    ACTIONS(318), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [1394] = 1,
    ACTIONS(320), 2,
      sym_assign_operator,
      sym_remove_operator,
  [1399] = 2,
    ACTIONS(322), 1,
      sym_arrow,
    ACTIONS(324), 1,
      sym_colon,
  [1406] = 1,
    ACTIONS(326), 2,
      aux_sym_newline_token1,
      sym_inline_comment,
  [1411] = 2,
    ACTIONS(328), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    STATE(34), 1,
      sym_fence_close,
  [1418] = 2,
    ACTIONS(330), 1,
      aux_sym_reference_token1,
    STATE(72), 1,
      sym_reference,
  [1425] = 2,
    ACTIONS(302), 1,
      anon_sym_BQUOTE_BQUOTE_BQUOTE,
    STATE(45), 1,
      sym_fence_open,
  [1432] = 1,
    ACTIONS(332), 1,
      sym_identifier,
  [1436] = 1,
    ACTIONS(334), 1,
      sym_colon,
  [1440] = 1,
    ACTIONS(336), 1,
      sym_identifier,
  [1444] = 1,
    ACTIONS(338), 1,
      sym_assign_operator,
  [1448] = 1,
    ACTIONS(340), 1,
      sym_colon,
  [1452] = 1,
    ACTIONS(342), 1,
      sym_colon,
  [1456] = 1,
    ACTIONS(344), 1,
      aux_sym_newline_token1,
  [1460] = 1,
    ACTIONS(346), 1,
      sym_colon,
  [1464] = 1,
    ACTIONS(348), 1,
      sym_rparen,
  [1468] = 1,
    ACTIONS(350), 1,
      sym_identifier,
  [1472] = 1,
    ACTIONS(352), 1,
      sym_colon,
  [1476] = 1,
    ACTIONS(354), 1,
      ts_builtin_sym_end,
  [1480] = 1,
    ACTIONS(356), 1,
      sym_identifier,
  [1484] = 1,
    ACTIONS(358), 1,
      sym_colon,
  [1488] = 1,
    ACTIONS(360), 1,
      sym_identifier,
  [1492] = 1,
    ACTIONS(362), 1,
      sym_colon,
  [1496] = 1,
    ACTIONS(364), 1,
      aux_sym_reference_token1,
  [1500] = 1,
    ACTIONS(366), 1,
      sym_identifier,
  [1504] = 1,
    ACTIONS(368), 1,
      sym_identifier,
};

static const uint32_t ts_small_parse_table_map[] = {
  [SMALL_STATE(2)] = 0,
  [SMALL_STATE(3)] = 45,
  [SMALL_STATE(4)] = 90,
  [SMALL_STATE(5)] = 135,
  [SMALL_STATE(6)] = 177,
  [SMALL_STATE(7)] = 219,
  [SMALL_STATE(8)] = 240,
  [SMALL_STATE(9)] = 261,
  [SMALL_STATE(10)] = 282,
  [SMALL_STATE(11)] = 303,
  [SMALL_STATE(12)] = 324,
  [SMALL_STATE(13)] = 345,
  [SMALL_STATE(14)] = 366,
  [SMALL_STATE(15)] = 387,
  [SMALL_STATE(16)] = 408,
  [SMALL_STATE(17)] = 429,
  [SMALL_STATE(18)] = 450,
  [SMALL_STATE(19)] = 471,
  [SMALL_STATE(20)] = 492,
  [SMALL_STATE(21)] = 513,
  [SMALL_STATE(22)] = 534,
  [SMALL_STATE(23)] = 555,
  [SMALL_STATE(24)] = 576,
  [SMALL_STATE(25)] = 597,
  [SMALL_STATE(26)] = 618,
  [SMALL_STATE(27)] = 639,
  [SMALL_STATE(28)] = 660,
  [SMALL_STATE(29)] = 681,
  [SMALL_STATE(30)] = 694,
  [SMALL_STATE(31)] = 707,
  [SMALL_STATE(32)] = 720,
  [SMALL_STATE(33)] = 733,
  [SMALL_STATE(34)] = 746,
  [SMALL_STATE(35)] = 759,
  [SMALL_STATE(36)] = 772,
  [SMALL_STATE(37)] = 795,
  [SMALL_STATE(38)] = 812,
  [SMALL_STATE(39)] = 829,
  [SMALL_STATE(40)] = 845,
  [SMALL_STATE(41)] = 859,
  [SMALL_STATE(42)] = 873,
  [SMALL_STATE(43)] = 883,
  [SMALL_STATE(44)] = 894,
  [SMALL_STATE(45)] = 905,
  [SMALL_STATE(46)] = 918,
  [SMALL_STATE(47)] = 931,
  [SMALL_STATE(48)] = 942,
  [SMALL_STATE(49)] = 955,
  [SMALL_STATE(50)] = 961,
  [SMALL_STATE(51)] = 971,
  [SMALL_STATE(52)] = 981,
  [SMALL_STATE(53)] = 987,
  [SMALL_STATE(54)] = 997,
  [SMALL_STATE(55)] = 1003,
  [SMALL_STATE(56)] = 1013,
  [SMALL_STATE(57)] = 1023,
  [SMALL_STATE(58)] = 1033,
  [SMALL_STATE(59)] = 1039,
  [SMALL_STATE(60)] = 1049,
  [SMALL_STATE(61)] = 1059,
  [SMALL_STATE(62)] = 1065,
  [SMALL_STATE(63)] = 1071,
  [SMALL_STATE(64)] = 1081,
  [SMALL_STATE(65)] = 1087,
  [SMALL_STATE(66)] = 1093,
  [SMALL_STATE(67)] = 1103,
  [SMALL_STATE(68)] = 1113,
  [SMALL_STATE(69)] = 1119,
  [SMALL_STATE(70)] = 1127,
  [SMALL_STATE(71)] = 1137,
  [SMALL_STATE(72)] = 1147,
  [SMALL_STATE(73)] = 1157,
  [SMALL_STATE(74)] = 1163,
  [SMALL_STATE(75)] = 1173,
  [SMALL_STATE(76)] = 1179,
  [SMALL_STATE(77)] = 1189,
  [SMALL_STATE(78)] = 1195,
  [SMALL_STATE(79)] = 1201,
  [SMALL_STATE(80)] = 1207,
  [SMALL_STATE(81)] = 1217,
  [SMALL_STATE(82)] = 1224,
  [SMALL_STATE(83)] = 1231,
  [SMALL_STATE(84)] = 1238,
  [SMALL_STATE(85)] = 1245,
  [SMALL_STATE(86)] = 1252,
  [SMALL_STATE(87)] = 1257,
  [SMALL_STATE(88)] = 1264,
  [SMALL_STATE(89)] = 1271,
  [SMALL_STATE(90)] = 1278,
  [SMALL_STATE(91)] = 1283,
  [SMALL_STATE(92)] = 1290,
  [SMALL_STATE(93)] = 1297,
  [SMALL_STATE(94)] = 1304,
  [SMALL_STATE(95)] = 1311,
  [SMALL_STATE(96)] = 1318,
  [SMALL_STATE(97)] = 1323,
  [SMALL_STATE(98)] = 1330,
  [SMALL_STATE(99)] = 1335,
  [SMALL_STATE(100)] = 1342,
  [SMALL_STATE(101)] = 1349,
  [SMALL_STATE(102)] = 1354,
  [SMALL_STATE(103)] = 1361,
  [SMALL_STATE(104)] = 1368,
  [SMALL_STATE(105)] = 1375,
  [SMALL_STATE(106)] = 1382,
  [SMALL_STATE(107)] = 1389,
  [SMALL_STATE(108)] = 1394,
  [SMALL_STATE(109)] = 1399,
  [SMALL_STATE(110)] = 1406,
  [SMALL_STATE(111)] = 1411,
  [SMALL_STATE(112)] = 1418,
  [SMALL_STATE(113)] = 1425,
  [SMALL_STATE(114)] = 1432,
  [SMALL_STATE(115)] = 1436,
  [SMALL_STATE(116)] = 1440,
  [SMALL_STATE(117)] = 1444,
  [SMALL_STATE(118)] = 1448,
  [SMALL_STATE(119)] = 1452,
  [SMALL_STATE(120)] = 1456,
  [SMALL_STATE(121)] = 1460,
  [SMALL_STATE(122)] = 1464,
  [SMALL_STATE(123)] = 1468,
  [SMALL_STATE(124)] = 1472,
  [SMALL_STATE(125)] = 1476,
  [SMALL_STATE(126)] = 1480,
  [SMALL_STATE(127)] = 1484,
  [SMALL_STATE(128)] = 1488,
  [SMALL_STATE(129)] = 1492,
  [SMALL_STATE(130)] = 1496,
  [SMALL_STATE(131)] = 1500,
  [SMALL_STATE(132)] = 1504,
};

static const TSParseActionEntry ts_parse_actions[] = {
  [0] = {.entry = {.count = 0, .reusable = false}},
  [1] = {.entry = {.count = 1, .reusable = false}}, RECOVER(),
  [3] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_source_file, 0, 0, 0),
  [5] = {.entry = {.count = 1, .reusable = true}}, SHIFT(29),
  [7] = {.entry = {.count = 1, .reusable = true}}, SHIFT(5),
  [9] = {.entry = {.count = 1, .reusable = true}}, SHIFT(42),
  [11] = {.entry = {.count = 1, .reusable = true}}, SHIFT(39),
  [13] = {.entry = {.count = 1, .reusable = true}}, SHIFT(132),
  [15] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk, 1, 0, 1),
  [17] = {.entry = {.count = 2, .reusable = false}}, REDUCE(sym_thunk, 1, 0, 1), SHIFT(8),
  [20] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk, 1, 0, 1),
  [22] = {.entry = {.count = 1, .reusable = false}}, SHIFT(108),
  [24] = {.entry = {.count = 1, .reusable = false}}, SHIFT(117),
  [26] = {.entry = {.count = 1, .reusable = false}}, SHIFT(56),
  [28] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk, 2, 0, 1),
  [30] = {.entry = {.count = 2, .reusable = false}}, REDUCE(sym_thunk, 2, 0, 1), SHIFT(8),
  [33] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk, 2, 0, 1),
  [35] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0),
  [37] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0), SHIFT_REPEAT(8),
  [40] = {.entry = {.count = 1, .reusable = false}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0),
  [42] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0), SHIFT_REPEAT(108),
  [45] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0), SHIFT_REPEAT(117),
  [48] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_thunk_repeat1, 2, 0, 0), SHIFT_REPEAT(56),
  [51] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_source_file, 1, 0, 0),
  [53] = {.entry = {.count = 1, .reusable = true}}, SHIFT(6),
  [55] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0),
  [57] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0), SHIFT_REPEAT(29),
  [60] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0), SHIFT_REPEAT(6),
  [63] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0), SHIFT_REPEAT(42),
  [66] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0), SHIFT_REPEAT(39),
  [69] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_source_file_repeat1, 2, 0, 0), SHIFT_REPEAT(132),
  [72] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 7, 0, 27),
  [74] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 7, 0, 27),
  [76] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_newline, 1, 0, 0),
  [78] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_newline, 1, 0, 0),
  [80] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 3, 0, 3),
  [82] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 3, 0, 3),
  [84] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_prompt_line, 2, 0, 4),
  [86] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_prompt_line, 2, 0, 4),
  [88] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_directive_line, 2, 0, 0),
  [90] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_directive_line, 2, 0, 0),
  [92] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 4, 0, 3),
  [94] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 4, 0, 3),
  [96] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 4, 0, 9),
  [98] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 4, 0, 9),
  [100] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 4, 0, 10),
  [102] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 4, 0, 10),
  [104] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_prompt_line, 3, 0, 4),
  [106] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_prompt_line, 3, 0, 4),
  [108] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_directive_line, 3, 0, 0),
  [110] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_directive_line, 3, 0, 0),
  [112] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 5, 0, 14),
  [114] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 5, 0, 14),
  [116] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 5, 0, 9),
  [118] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 5, 0, 9),
  [120] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 5, 0, 15),
  [122] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 5, 0, 15),
  [124] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 5, 0, 10),
  [126] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 5, 0, 10),
  [128] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 6, 0, 14),
  [130] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 6, 0, 14),
  [132] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 6, 0, 20),
  [134] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 6, 0, 20),
  [136] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 6, 0, 15),
  [138] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 6, 0, 15),
  [140] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 6, 0, 21),
  [142] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 6, 0, 21),
  [144] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 7, 0, 20),
  [146] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 7, 0, 20),
  [148] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 7, 0, 21),
  [150] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 7, 0, 21),
  [152] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_header, 8, 0, 27),
  [154] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_thunk_header, 8, 0, 27),
  [156] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_blank_line, 1, 0, 0),
  [158] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_blank_line, 1, 0, 0),
  [160] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_use_statement, 4, 0, 7),
  [162] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_use_statement, 5, 0, 7),
  [164] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_fence_close, 2, 0, 0),
  [166] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_declaration, 3, 0, 5),
  [168] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_declaration, 2, 0, 2),
  [170] = {.entry = {.count = 1, .reusable = false}}, SHIFT(78),
  [172] = {.entry = {.count = 1, .reusable = false}}, SHIFT(120),
  [174] = {.entry = {.count = 1, .reusable = false}}, SHIFT(105),
  [176] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_fence_body_repeat1, 2, 0, 0), SHIFT_REPEAT(78),
  [179] = {.entry = {.count = 1, .reusable = false}}, REDUCE(aux_sym_fence_body_repeat1, 2, 0, 0),
  [181] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_fence_body_repeat1, 2, 0, 0), SHIFT_REPEAT(105),
  [184] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_fence_body, 1, 0, 0),
  [186] = {.entry = {.count = 1, .reusable = true}}, SHIFT(123),
  [188] = {.entry = {.count = 1, .reusable = true}}, SHIFT(74),
  [190] = {.entry = {.count = 1, .reusable = true}}, SHIFT(126),
  [192] = {.entry = {.count = 1, .reusable = true}}, SHIFT(48),
  [194] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_model_directive, 2, 0, 6),
  [196] = {.entry = {.count = 1, .reusable = true}}, SHIFT(52),
  [198] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_collection_directive, 2, 0, 6),
  [200] = {.entry = {.count = 1, .reusable = true}}, SHIFT(130),
  [202] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_directive_values, 1, 0, 12),
  [204] = {.entry = {.count = 1, .reusable = true}}, SHIFT(87),
  [206] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_directive_values, 2, 0, 16),
  [208] = {.entry = {.count = 1, .reusable = true}}, SHIFT(78),
  [210] = {.entry = {.count = 1, .reusable = true}}, SHIFT(103),
  [212] = {.entry = {.count = 1, .reusable = true}}, SHIFT(67),
  [214] = {.entry = {.count = 1, .reusable = true}}, SHIFT(88),
  [216] = {.entry = {.count = 1, .reusable = true}}, SHIFT(53),
  [218] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_directive_values_repeat1, 2, 0, 22),
  [220] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_directive_values_repeat1, 2, 0, 22), SHIFT_REPEAT(87),
  [223] = {.entry = {.count = 1, .reusable = true}}, SHIFT(114),
  [225] = {.entry = {.count = 1, .reusable = true}}, SHIFT(80),
  [227] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 7, 0, 23),
  [229] = {.entry = {.count = 1, .reusable = true}}, SHIFT(8),
  [231] = {.entry = {.count = 1, .reusable = true}}, SHIFT(97),
  [233] = {.entry = {.count = 1, .reusable = true}}, SHIFT(104),
  [235] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_directive_value, 1, 0, 0),
  [237] = {.entry = {.count = 1, .reusable = true}}, SHIFT(100),
  [239] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 5, 0, 17),
  [241] = {.entry = {.count = 1, .reusable = true}}, SHIFT(115),
  [243] = {.entry = {.count = 1, .reusable = true}}, SHIFT(91),
  [245] = {.entry = {.count = 1, .reusable = true}}, SHIFT(83),
  [247] = {.entry = {.count = 1, .reusable = true}}, SHIFT(89),
  [249] = {.entry = {.count = 1, .reusable = true}}, SHIFT(92),
  [251] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_fence_content_line, 2, 0, 4),
  [253] = {.entry = {.count = 1, .reusable = true}}, SHIFT(106),
  [255] = {.entry = {.count = 1, .reusable = true}}, SHIFT(85),
  [257] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_fence_open, 1, 0, 0),
  [259] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_directive_values_repeat1, 2, 0, 8),
  [261] = {.entry = {.count = 1, .reusable = true}}, SHIFT(84),
  [263] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 6, 0, 17),
  [265] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 6, 0, 23),
  [267] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_parameter_list_repeat1, 2, 0, 25),
  [269] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_parameter_list_repeat1, 2, 0, 25), SHIFT_REPEAT(91),
  [272] = {.entry = {.count = 1, .reusable = true}}, SHIFT(81),
  [274] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 6, 0, 26),
  [276] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_parameter, 1, 0, 13),
  [278] = {.entry = {.count = 1, .reusable = true}}, SHIFT(90),
  [280] = {.entry = {.count = 1, .reusable = true}}, SHIFT(121),
  [282] = {.entry = {.count = 1, .reusable = true}}, SHIFT(82),
  [284] = {.entry = {.count = 1, .reusable = true}}, SHIFT(93),
  [286] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 7, 0, 26),
  [288] = {.entry = {.count = 1, .reusable = true}}, SHIFT(102),
  [290] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 7, 0, 28),
  [292] = {.entry = {.count = 1, .reusable = true}}, SHIFT(94),
  [294] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_declaration_header, 8, 0, 28),
  [296] = {.entry = {.count = 1, .reusable = false}}, REDUCE(sym_fence_content_line, 1, 0, 0),
  [298] = {.entry = {.count = 1, .reusable = true}}, SHIFT(99),
  [300] = {.entry = {.count = 1, .reusable = true}}, SHIFT(41),
  [302] = {.entry = {.count = 1, .reusable = true}}, SHIFT(61),
  [304] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_parameter, 2, 0, 18),
  [306] = {.entry = {.count = 1, .reusable = true}}, SHIFT(69),
  [308] = {.entry = {.count = 1, .reusable = true}}, SHIFT(128),
  [310] = {.entry = {.count = 1, .reusable = true}}, SHIFT(51),
  [312] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_reference, 1, 0, 0),
  [314] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_thunk_input, 3, 0, 8),
  [316] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_parameter_list_repeat1, 2, 0, 19),
  [318] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_model_directive, 3, 0, 11),
  [320] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_collection_subject, 1, 0, 0),
  [322] = {.entry = {.count = 1, .reusable = true}}, SHIFT(131),
  [324] = {.entry = {.count = 1, .reusable = true}}, SHIFT(63),
  [326] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_collection_directive, 3, 0, 11),
  [328] = {.entry = {.count = 1, .reusable = true}}, SHIFT(120),
  [330] = {.entry = {.count = 1, .reusable = true}}, SHIFT(96),
  [332] = {.entry = {.count = 1, .reusable = true}}, SHIFT(124),
  [334] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_parameter_list, 4, 0, 24),
  [336] = {.entry = {.count = 1, .reusable = true}}, SHIFT(57),
  [338] = {.entry = {.count = 1, .reusable = true}}, SHIFT(40),
  [340] = {.entry = {.count = 1, .reusable = true}}, SHIFT(50),
  [342] = {.entry = {.count = 1, .reusable = true}}, SHIFT(59),
  [344] = {.entry = {.count = 1, .reusable = true}}, SHIFT(33),
  [346] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_parameter_list, 3, 0, 19),
  [348] = {.entry = {.count = 1, .reusable = true}}, SHIFT(98),
  [350] = {.entry = {.count = 1, .reusable = true}}, SHIFT(127),
  [352] = {.entry = {.count = 1, .reusable = true}}, SHIFT(76),
  [354] = {.entry = {.count = 1, .reusable = true}},  ACCEPT_INPUT(),
  [356] = {.entry = {.count = 1, .reusable = true}}, SHIFT(122),
  [358] = {.entry = {.count = 1, .reusable = true}}, SHIFT(71),
  [360] = {.entry = {.count = 1, .reusable = true}}, SHIFT(118),
  [362] = {.entry = {.count = 1, .reusable = true}}, SHIFT(113),
  [364] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_cap_kind, 1, 0, 0),
  [366] = {.entry = {.count = 1, .reusable = true}}, SHIFT(119),
  [368] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_decl_kind, 1, 0, 0),
};

#ifdef __cplusplus
extern "C" {
#endif
#ifdef TREE_SITTER_HIDE_SYMBOLS
#define TS_PUBLIC
#elif defined(_WIN32)
#define TS_PUBLIC __declspec(dllexport)
#else
#define TS_PUBLIC __attribute__((visibility("default")))
#endif

TS_PUBLIC const TSLanguage *tree_sitter_toolang(void) {
  static const TSLanguage language = {
    .version = LANGUAGE_VERSION,
    .symbol_count = SYMBOL_COUNT,
    .alias_count = ALIAS_COUNT,
    .token_count = TOKEN_COUNT,
    .external_token_count = EXTERNAL_TOKEN_COUNT,
    .state_count = STATE_COUNT,
    .large_state_count = LARGE_STATE_COUNT,
    .production_id_count = PRODUCTION_ID_COUNT,
    .field_count = FIELD_COUNT,
    .max_alias_sequence_length = MAX_ALIAS_SEQUENCE_LENGTH,
    .parse_table = &ts_parse_table[0][0],
    .small_parse_table = ts_small_parse_table,
    .small_parse_table_map = ts_small_parse_table_map,
    .parse_actions = ts_parse_actions,
    .symbol_names = ts_symbol_names,
    .field_names = ts_field_names,
    .field_map_slices = ts_field_map_slices,
    .field_map_entries = ts_field_map_entries,
    .symbol_metadata = ts_symbol_metadata,
    .public_symbol_map = ts_symbol_map,
    .alias_map = ts_non_terminal_alias_map,
    .alias_sequences = &ts_alias_sequences[0][0],
    .lex_modes = ts_lex_modes,
    .lex_fn = ts_lex,
    .primary_state_ids = ts_primary_state_ids,
  };
  return &language;
}
#ifdef __cplusplus
}
#endif
