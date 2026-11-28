<?php
declare(strict_types=1);
/* Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0 */

/* Read evaluated make variables. This check never invokes a build or install target. */
function portMetadata(string $port, string $ports, string $root): array {
	$names = ['PKGBASE', 'PKGNAME', 'IGNORE', 'USE_PHP', 'PHP_EXT_INC', 'PHP_VER', 'RUN_DEPENDS', 'TEST_DEPENDS', 'FLAVORS', 'PHP_SAPI'];
	$command = ['/usr/bin/make', '-m', '/usr/share/mk', '-m', "$ports/Mk", '-C', $port,
		"PORTSDIR=$ports", 'FREESENSE_PACKAGE_TRAIN=2.0', "FREESENSE_REFACTOR_SRC=$root/freesense/src", "FREESENSE_NATIVE_SRC=$root/freesense-native"];
	foreach ($names as $name) {
		array_push($command, '-V', '${' . $name . '}');
	}
	$process = proc_open($command, [1 => ['pipe', 'w']], $pipes);
	if (!is_resource($process)) {
		throw new RuntimeException("Could not query $port.");
	}
	$result = stream_get_contents($pipes[1]);
	fclose($pipes[1]);
	if (proc_close($process) !== 0 || $result === false) {
		throw new RuntimeException("Ports metadata query failed for $port.");
	}
	$rows = explode("\n", $result);
	if (count($rows) !== count($names) + 1 || array_pop($rows) !== '') {
		throw new RuntimeException("Unexpected ports metadata for $port.");
	}
	$data = array_combine($names, array_map(trim(...), $rows));
	if ($data['IGNORE'] !== '') {
		throw new RuntimeException("Port ignored: $port: " . $data['IGNORE']);
	}
	return $data;
}

if (PHP_OS !== 'FreeBSD' || count($argv) !== 2 || !is_file($argv[1] . '/Mk/Uses/php.mk')) {
	fwrite(STDERR, "Usage on FreeBSD: php tools/php-ports.php /absolute/path/to/ports\n");
	exit(1);
}
$root = dirname(__DIR__);
$ports = realpath($argv[1]);
$providers = [];
foreach (['freesense-system-ports', 'freesense-packages'] as $component) {
	foreach (glob("$root/$component/*/*/Makefile") as $file) {
		$port = dirname($file);
		$data = portMetadata($port, $ports, $root);
		$origin = substr($port, strlen($root) + 1);
		$extensions = $data['USE_PHP'] === '' ? [] : preg_split('/\s+/', $data['USE_PHP']);
		if ($data['PHP_VER'] !== '' && ($data['PHP_VER'] !== '86' || $data['FLAVORS'] !== 'php86')) {
			throw new RuntimeException("Expected the php86 flavor in $origin.");
		}
		$dependency = [];
		foreach (['RUN_DEPENDS', 'TEST_DEPENDS'] as $phase) {
			$dependency[$phase] = [];
			foreach (preg_split('/\s+/', $data[$phase], -1, PREG_SPLIT_NO_EMPTY) as $entry) {
				$parts = explode(':', $entry);
				if (!isset($parts[1]) || !preg_match('~/php(?:[0-9]|$|@|-)~', $parts[1])) {
					continue;
				}
				if (!preg_match('~^[a-z0-9-]+/(php86(?:-[a-z0-9_]+)?)$~D', $parts[1], $match)) {
					throw new RuntimeException("Unversioned or incompatible PHP origin in $origin: " . $parts[1]);
				}
				$name = $match[1];
				$provider = $providers[$parts[1]] ??= portMetadata("$ports/$parts[1]", $ports, $root);
				if ($provider['PKGBASE'] !== $name || $provider['PHP_VER'] !== '86'
					|| ($name === 'php86' && !in_array('cli', explode(' ', $provider['PHP_SAPI']), true))) {
					throw new RuntimeException("Unexpected PHP provider at $parts[1].");
				}
				$dependency[$phase][] = $name;
			}
		}
		// Extensions that are part of php86 itself (openssl, hash, ...) have no package of their own.
		foreach (array_diff($extensions, explode(' ', $data['PHP_EXT_INC'])) as $extension) {
			if (!in_array("php86-$extension", $dependency['RUN_DEPENDS'], true)) {
				throw new RuntimeException("Missing versioned runtime provider for $origin $extension.");
			}
		}
		if ($data['PKGBASE'] === 'FreeSense-system'
			&& (array_diff(['php86', 'php86-ctype', 'php86-dom', 'php86-filter', 'php86-pcntl', 'php86-posix', 'php86-sqlite3'], $dependency['RUN_DEPENDS'])
			|| !in_array('php86-tokenizer', $dependency['TEST_DEPENDS'], true)
			|| in_array('php86-tokenizer', $dependency['RUN_DEPENDS'], true))) {
			throw new RuntimeException('System runtime or test-only PHP dependency mismatch.');
		}
		echo "$origin\n  package ", $data['PKGNAME'], "\n  runtime ", $data['RUN_DEPENDS'] ?: '(none)',
			"\n  tests ", $data['TEST_DEPENDS'] ?: '(none)', "\n";
	}
}
foreach ($providers as $origin => $data) {
	echo "provider $origin ", $data['PKGNAME'], "\n";
}
