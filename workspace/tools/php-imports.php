<?php
declare(strict_types=1);
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 The FreeSense Project.
// Missing imports: a class name that resolves into FreeSense\ but that no shipped file declares. PHP only reports it when
// the line runs, so a forgotten `use` in a native-only path passes lint and the portable tests.
// Usage: php php-imports.php ROOT  (every PHP file under ROOT's src/ and files/ trees)

/** PHP files that are installed on an appliance. @return list<string> */
function shippedFiles(string $root): array {
	$found = [];
	$filter = new RecursiveCallbackFilterIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
		static fn(SplFileInfo $file): bool => !$file->isDir()
			|| !in_array($file->getFilename(), ['.git', '__pycache__', 'node_modules', 'vendor', 'tests', 'planning', 'local-output', 'third-party'], true));
	foreach (new RecursiveIteratorIterator($filter) as $file) {
		$path = str_replace('\\', '/', $file->getPathname());
		if (!preg_match('#/(src|files)/#', substr($path, strlen($root)))) {
			continue;
		}
		$extension = $file->getExtension();
		if (in_array($extension, ['php', 'inc', 'phtml'], true)
			|| ($extension === '' && preg_match('/\A(<\?php\b|#![^\n]*\bphp\b)/', (string)file_get_contents($path, length: 128)))) {
			$found[] = $path;
		}
	}
	sort($found);
	return $found;
}

/**
 * Resolved class names of one file. A name probed with class_exists() is optional code and is not reported for that file.
 * @return array{declared:list<string>,references:list<array{string,int}>,optional:array<string,true>}
 */
function classNames(string $code): array {
	$tokens = array_values(array_filter(PhpToken::tokenize($code),
		static fn(PhpToken $t): bool => !$t->is([T_WHITESPACE, T_COMMENT, T_DOC_COMMENT, T_INLINE_HTML, T_OPEN_TAG, T_CLOSE_TAG])));
	$names = [T_STRING, T_NAME_QUALIFIED, T_NAME_FULLY_QUALIFIED];
	$builtin = ['self', 'static', 'parent', 'int', 'string', 'array', 'bool', 'float', 'void', 'never', 'null', 'false', 'true', 'mixed',
		'object', 'iterable', 'callable'];
	$namespace = '';
	$aliases = $declared = $references = [];
	$depth = 0;
	$count = count($tokens);
	$text = static fn(int $i): string => $i >= 0 && $i < $count ? $tokens[$i]->text : '';
	$isName = static fn(int $i): bool => $i >= 0 && $i < $count && $tokens[$i]->is($names);

	$optional = [];
	$use = static function (int $i) use (&$namespace, &$aliases, &$references, $names, $builtin, $tokens): void {
		$name = $tokens[$i]->text;
		if (!$tokens[$i]->is($names) || in_array(strtolower($name), $builtin, true)) {
			return;
		}
		if ($name[0] === '\\') {
			$resolved = substr($name, 1);
		} else {
			[$first, $rest] = array_pad(explode('\\', $name, 2), 2, null);
			$resolved = isset($aliases[strtolower($first)])
				? $aliases[strtolower($first)] . ($rest === null ? '' : '\\' . $rest)
				: ($namespace === '' ? $name : $namespace . '\\' . $name);
		}
		$references[] = [$resolved, $tokens[$i]->line];
	};
	/** A type expression starting at $i: names joined by | & ( ) with an optional ?. */
	$type = static function (int $i) use ($use, $text, $isName): void {
		while (in_array($text($i), ['?', '|', '&', '(', ')'], true) || $isName($i) || in_array($text($i), ['static', 'array', 'callable'], true)) {
			if ($isName($i)) {
				$use($i);
			}
			$i++;
		}
	};

	for ($i = 0; $i < $count; $i++) {
		$token = $tokens[$i];
		if ($token->text === '{' || $token->is([T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES])) {
			$depth++;
		} elseif ($token->text === '}') {
			$depth--;
		} elseif ($token->is(T_NAMESPACE) && $isName($i + 1)) {
			$namespace = $text(++$i);
			$aliases = [];
		} elseif ($token->is(T_USE) && $text($i + 1) !== '(') {
			if ($depth > 0) {
				// A trait inside a class body.
				for ($j = $i + 1; $isName($j) || $text($j) === ','; $j++) {
					if ($isName($j)) {
						$use($j);
					}
				}
				continue;
			}
			if ($tokens[$i + 1]->is([T_FUNCTION, T_CONST])) {
				while ($i < $count && $text($i) !== ';') {
					$i++;
				}
				continue;
			}
			$prefix = '';
			for ($i++; $i < $count && $text($i) !== ';'; $i++) {
				if ($tokens[$i]->is(T_NS_SEPARATOR) && $text($i + 1) === '{') {
					$prefix = ltrim($text($i - 1), '\\') . '\\';
					continue;
				}
				if (!$isName($i) || $tokens[$i + 1]->is(T_NS_SEPARATOR)) {
					continue;
				}
				$full = $prefix . ltrim($text($i), '\\');
				$alias = basename(str_replace('\\', '/', $full));
				if ($tokens[$i + 1]->is(T_AS)) {
					$alias = $text($i + 2);
					$i += 2;
				}
				$aliases[strtolower($alias)] = $full;
			}
		} elseif ($token->is([T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM]) && $tokens[$i + 1]->is(T_STRING)) {
			$declared[] = ($namespace === '' ? '' : $namespace . '\\') . $text($i + 1);
			$i++;
		} elseif ($token->is([T_NEW, T_INSTANCEOF, T_ATTRIBUTE])) {
			$isName($i + 1) && $use($i + 1);
		} elseif ($token->is([T_EXTENDS, T_IMPLEMENTS])) {
			for ($j = $i + 1; $isName($j) || $text($j) === ','; $j++) {
				$isName($j) && $use($j);
			}
		} elseif ($token->is(T_CATCH)) {
			$type($i + 2);
		} elseif ($token->is(T_DOUBLE_COLON)) {
			if ($isName($i - 1) && !in_array($text($i - 2), ['->', '?->', '::'], true)) {
				$use($i - 1);
				if ($text($i - 2) === '(' && in_array(ltrim($text($i - 3), "\x5c"), ['class_exists', 'interface_exists'], true) && $references) {
					$optional[strtolower($references[array_key_last($references)][0])] = true;
				}
			}
		} elseif ($token->is([T_VARIABLE, T_ELLIPSIS, T_AMPERSAND_FOLLOWED_BY_VAR_OR_VARARG])) {
			// A parameter or property type: walk back over a union or intersection.
			if ($token->is(T_VARIABLE) && $i > 0 && $tokens[$i - 1]->is([T_ELLIPSIS, T_AMPERSAND_FOLLOWED_BY_VAR_OR_VARARG])) {
				continue;
			}
			for ($j = $i - 1; $isName($j); $j -= 2) {
				$use($j);
				if (!in_array($text($j - 1), ['|', '&'], true)) {
					break;
				}
			}
		} elseif ($token->is([T_FUNCTION, T_FN])) {
			for ($j = $i + 1; $j < $count && $text($j) !== '('; $j++) {
			}
			for ($open = 0; $j < $count; $j++) {
				$open += $text($j) === '(' ? 1 : ($text($j) === ')' ? -1 : 0);
				if ($open === 0) {
					break;
				}
			}
			$j++;
			if ($j < $count && $tokens[$j]->is(T_USE)) {
				while ($j < $count && $text($j) !== ')') {
					$j++;
				}
				$j++;
			}
			if ($text($j) === ':') {
				$type($j + 1);
			}
		}
	}
	return ['declared' => $declared, 'references' => $references, 'optional' => $optional];
}

$root = rtrim(str_replace('\\', '/', $argv[1] ?? ''), '/');
if ($root === '' || !is_dir($root)) {
	fwrite(STDERR, "usage: php php-imports.php ROOT\n");
	exit(2);
}
$known = $used = [];
foreach (shippedFiles($root) as $path) {
	$names = classNames((string)file_get_contents($path));
	foreach ($names['declared'] as $name) {
		$known[strtolower($name)] = true;
	}
	$used[substr($path, strlen($root) + 1)] = array_filter($names['references'],
		static fn(array $r): bool => !isset($names['optional'][strtolower($r[0])]));
}
$missing = [];
foreach ($used as $file => $references) {
	foreach ($references as [$name, $line]) {
		if (str_starts_with($name, 'FreeSense\\') && !isset($known[strtolower($name)])) {
			$missing["$file: $name"] ??= "$file:$line: no shipped file declares $name (missing use statement?)";
		}
	}
}
foreach ($missing as $report) {
	fwrite(STDERR, $report . "\n");
}
printf("Imports: %d shipped PHP files, %d unresolved class names\n", count($used), count($missing));
exit($missing ? 1 : 0);
