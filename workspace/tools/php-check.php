<?php
declare(strict_types=1);
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 The FreeSense Project.
// Run each test with a fresh symbol table and collect startup and compile diagnostics.

if (PHP_MAJOR_VERSION !== 8 || PHP_MINOR_VERSION !== 6) {
	fwrite(STDERR, 'PHP 8.6 CLI required, found ' . PHP_VERSION . PHP_EOL);
	exit(1);
}
if (($argv[1] ?? '') === 'preflight') {
	$extensions = is_file(__DIR__ . '/modes.json') ? ['ctype', 'dom', 'filter', 'sqlite3', 'tokenizer'] : ['tokenizer'];
	foreach ($extensions as $extension) {
		if (!extension_loaded($extension)) {
			throw new RuntimeException("PHP 8.6 CLI extension required: $extension");
		}
	}
	exit(0);
}
error_reporting(E_ALL);
set_error_handler(static function (int $level, string $message, string $file, int $line): never {
	throw new ErrorException($message, 0, $level, $file, $line);
});
$appliance = ($argv[1] ?? '') === 'appliance';
if ($appliance && PHP_OS !== 'FreeBSD') {
	throw new RuntimeException('Native FreeBSD runtime required');
}
printf("PHP %s %s %s\n", PHP_VERSION, PHP_OS, php_uname('m'));

/** Remove one invocation's temporary directory; links are removed, never followed. */
function removeTree(string $path): void {
	if (!is_dir($path) || is_link($path)) {
		@unlink($path);
		return;
	}
	@chmod($path, 0700);
	foreach (array_diff(@scandir($path) ?: [], ['.', '..']) as $name) {
		removeTree($path . '/' . $name);
	}
	@rmdir($path);
}

/**
 * Run PHP invocations side by side and fail on diagnostics even when PHP exits successfully. Every invocation gets its own
 * temporary directory and its output is collected, so all failures are reported complete after the last one finishes.
 *
 * @param array<string,list<string>> $runs label => PHP arguments
 * @return array<string,array{seconds:float,output:string,failed:bool}> in the order of $runs
 */
function runPhp(array $runs, int $workers, ?callable $done = null): array {
	$flags = ['-d', 'error_reporting=E_ALL', '-d', 'display_errors=stderr', '-d', 'display_startup_errors=1', '-d', 'log_errors=0',
		'-d', 'zend.assertions=1'];
	$queue = $runs;
	$active = $results = [];
	static $number = 0;
	while ($queue || $active) {
		while ($queue && count($active) < $workers) {
			$label = array_key_first($queue);
			$args = $queue[$label];
			unset($queue[$label]);
			$environment = $temporary = null;
			if ($workers > 1) {
				// Short names: tests put Unix sockets below their temporary directory.
				$temporary = sys_get_temp_dir() . '/c' . getmypid() . '-' . ++$number;
				if (!mkdir($temporary, 0777) || !chmod($temporary, 01777)) {
					throw new RuntimeException('Cannot create a temporary directory for a check');
				}
				$environment = ['TMPDIR' => $temporary] + getenv();
			}
			$log = tmpfile();
			if ($log === false) {
				throw new RuntimeException('Cannot open a diagnostic stream');
			}
			$process = proc_open([PHP_BINARY, ...$flags, ...$args], [1 => $log, 2 => $log], $pipes, null, $environment);
			if ($process === false) {
				throw new RuntimeException('Cannot launch PHP');
			}
			$active[$label] = [$process, $log, microtime(true), $temporary];
		}
		$finished = false;
		foreach ($active as $label => [$process, $log, $begin, $temporary]) {
			$status = proc_get_status($process);
			if ($status['running']) {
				continue;
			}
			proc_close($process);
			rewind($log);
			$output = stream_get_contents($log);
			fclose($log);
			if ($temporary !== null) {
				removeTree($temporary);
			}
			unset($active[$label]);
			$finished = true;
			$results[$label] = ['seconds' => microtime(true) - $begin, 'output' => (string)$output,
				'failed' => $output === false || $status['exitcode'] !== 0
					|| preg_match('/^(?:PHP )?(?:Deprecated|Warning|Notice|Fatal error|Parse error):/im', $output) === 1];
			if ($done) {
				$done($label, $results[$label]);
			}
		}
		if (!$finished) {
			usleep(5000);
		}
	}
	return array_replace($runs, $results);
}

/** Appliance tests share one kernel and run one at a time. */
function workers(bool $appliance = false): int {
	if ($appliance) {
		return 1;
	}
	$set = (int)getenv('FREESENSE_PHP_JOBS');
	if ($set > 0) {
		return $set;
	}
	$count = is_readable('/proc/cpuinfo') ? preg_match_all('/^processor\s*:/m', (string)file_get_contents('/proc/cpuinfo')) : 2;
	return max(1, min(12, (int)($count / 2)));
}

/** Print the failures in full and stop with a non-zero status; otherwise the slowest tests. */
function reportRuns(array $results, string $kind): void {
	$failed = array_filter($results, static fn(array $r): bool => $r['failed']);
	foreach ($failed as $label => $result) {
		fwrite(STDERR, "\n----- FAILED $kind: $label -----\n" . rtrim($result['output']) . "\n");
	}
	if ($failed) {
		fwrite(STDERR, sprintf("\n%d of %d %s failed: %s\n", count($failed), count($results), $kind, implode(', ', array_keys($failed))));
		exit(1);
	}
}

/** `php -l` takes many files at once; a batch costs one PHP start. */
function lint(array $files): void {
	$runs = [];
	foreach (array_chunk($files, 40) as $number => $chunk) {
		$runs['lint batch ' . ($number + 1)] = ['-l', ...$chunk];
	}
	reportRuns(runPhp($runs, workers()), 'lint batches');
}

/** @param array<string,string> $files label => path */
function runTests(array $files, bool $appliance, bool $each): void {
	$started = microtime(true);
	$results = runPhp(array_map(static fn(string $file): array => [$file], $files), workers($appliance),
		$each ? static function (string $label, array $result): void {
			printf("%s %6.1fs  %s\n", $result['failed'] ? 'FAIL' : 'ok  ', $result['seconds'], $label);
		} : null);
	if ($appliance) {
		foreach ($results as $result) {
			echo $result['failed'] ? '' : $result['output'];
		}
	}
	reportRuns($results, 'tests');
	uasort($results, static fn(array $a, array $b): int => $b['seconds'] <=> $a['seconds']);
	$slowest = [];
	foreach (array_slice($results, 0, 5, true) as $label => $result) {
		$slowest[] = sprintf('%s %.1fs', $label, $result['seconds']);
	}
	printf("%d tests passed in %.1fs with %d workers. Slowest: %s\n", count($results), microtime(true) - $started,
		workers($appliance), implode(', ', $slowest));
}

if (($argv[1] ?? '') === 'lint') {
	lint(array_slice($argv, 2));
	exit(0);
}

if (($argv[1] ?? '') === 'selected') {
	$files = array_slice($argv, 2);
	if (!$files) {
		throw new RuntimeException('Choose at least one portable PHP test.');
	}
	runTests(array_combine($files, $files), false, true);
	printf("Passed %d selected portable PHP tests\n", count($files));
	exit(0);
}

/** Extract named types without loading code or mistaking ::class for a declaration. */
function declaredTypes(array $tokens): array {
	$types = [];
	foreach ($tokens as $pos => $token) {
		if (!is_array($token) || !in_array($token[0], [T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM], true)) {
			continue;
		}
		do {
			$next = $tokens[++$pos] ?? null;
		} while (is_array($next) && in_array($next[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true));
		if (is_array($next) && $next[0] === T_STRING) {
			$types[] = $next[1];
		}
	}
	return $types;
}

$component = __DIR__ . '/component';
$install = $argv[2] ?? '';
if (is_file(__DIR__ . '/modes.json')) {
	$modes = json_decode(file_get_contents(__DIR__ . '/modes.json'), true, flags: JSON_THROW_ON_ERROR);
	foreach ($modes as $path => $mode) {
		if ((fileperms($install . $path) & 07777) !== $mode
			|| hash_file('sha256', $install . $path) !== hash_file('sha256', $component . '/src' . $path)) {
			throw new RuntimeException("Installed mode or content mismatch: $path");
		}
	}
	printf("System staging verified %d file modes and digests\n", count($modes));
}
$sources = json_decode(file_get_contents(__DIR__ . '/paths.json'), true, flags: JSON_THROW_ON_ERROR);
$classes = [];
$tests = [];
$classPrefix = 'src/usr/local/FreeSense/lib/';
lint(array_map(static fn(string $relative): string => $component . '/' . $relative, $sources));
foreach ($sources as $relative) {
	$file = $component . '/' . $relative;
	$code = file_get_contents($file);
	$tokens = token_get_all($code);
	$compact = '';
	foreach ($tokens as $token) {
		if (!is_array($token)) {
			$compact .= $token;
		} elseif (!in_array($token[0], [T_COMMENT, T_DOC_COMMENT, T_WHITESPACE, T_INLINE_HTML], true)) {
			$compact .= $token[1];
		}
	}
	if (!preg_match('/^<\?php\s*declare\(strict_types=1\);/', $compact)) {
		throw new RuntimeException("Missing leading strict_types declaration: $relative");
	}
	if (str_starts_with($relative, $classPrefix)) {
		$type = substr($relative, strlen($classPrefix), -4);
		$name = basename($type);
		if (!str_ends_with($relative, '.php') || !preg_match('/^[A-Z][A-Za-z0-9]*$/D', $name)
			|| declaredTypes($tokens) !== [$name]) {
			throw new RuntimeException("PSR-4 file must declare its named type: $relative");
		}
		$classes['FreeSense\\' . str_replace('/', '\\', $type)] = substr($relative, 4);
	} elseif (is_file(__DIR__ . '/modes.json') && str_starts_with($relative, 'src/') && declaredTypes($tokens)) {
		throw new RuntimeException("System type declared outside the PSR-4 root: $relative");
	}
	if (str_starts_with($relative, 'tests/') && str_ends_with($relative, 'Test.php')
		&& str_starts_with($relative, 'tests/appliance/') === $appliance) {
		$tests[$relative] = $file;
	}
}
if ($classes) {
	// Use only the staged install tree here. Validate actual autoload behavior including case.
	require $install . '/usr/local/FreeSense/autoload.php';
	foreach ($classes as $type => $file) {
		$reflection = new ReflectionClass($type);
		if ($reflection->getName() !== $type ||
			realpath((string)$reflection->getFileName()) !== realpath($install . '/' . $file)) {
			throw new RuntimeException("Incorrect autoload mapping for $type");
		}
	}
}
if (!$tests) {
	throw new RuntimeException($appliance ? 'No native appliance acceptance tests found' : 'No portable PHP tests found');
}
ksort($tests);
runTests($tests, $appliance, false);
printf("Validated %d PHP files, %d autoloaded types, %d %s tests\n",
	count($sources), count($classes), count($tests), $appliance ? 'appliance' : 'portable');
