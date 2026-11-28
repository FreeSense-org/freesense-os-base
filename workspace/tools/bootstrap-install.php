<?php
declare(strict_types=1);
/* Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0 */
/* Install one immutable System payload into a pristine disposable guest. Invoked by bootstrap.py. */

function bootRequire(bool $ok, string $message): void {
	if (!$ok) {
		throw new RuntimeException($message);
	}
}

/* Refuse symlinks, special files and writable parent directories even on repeat installations. */
function bootParents(string $root, string $path): void {
	$parent = $root;
	foreach (explode('/', trim(dirname($path), '/')) as $part) {
		$parent .= '/' . $part;
		bootRequire(!is_link($parent), "Symlink in install path: $parent");
		if (file_exists($parent)) {
			bootRequire(is_dir($parent) && fileowner($parent) === 0 && (fileperms($parent) & 0022) === 0,
				"Untrusted install directory: $parent");
		}
	}
}

function bootCreateDir(string $dir, int $mode): void {
	if (!is_dir($dir)) {
		$old = umask(0022);
		try {
			bootRequire(mkdir($dir, $mode, true), "Cannot create $dir");
		} finally {
			umask($old);
		}
	}
}

/* link() commits an already complete file and refuses to replace an intervening file. */
function bootCreateFile(string $path, string $bytes, int $mode): void {
	bootCreateDir(dirname($path), 0755);
	$tmp = tempnam(dirname($path), '.bootstrap-');
	bootRequire($tmp !== false && dirname($tmp) === dirname($path), "Cannot stage $path");
	try {
		bootRequire(file_put_contents($tmp, $bytes) === strlen($bytes) && chmod($tmp, $mode), "Cannot stage contents of $path");
		bootRequire(link($tmp, $path), "Cannot create $path");
	} finally {
		unlink($tmp);
	}
}

function bootMatches(string $path, array $attributes): bool {
	return is_file($path) && !is_link($path) && fileowner($path) === 0 && filegroup($path) === 0
		&& (fileperms($path) & 07777) === $attributes['mode']
		&& hash_file('sha256', $path) === $attributes['sha256'];
}

/** $root is an empty prefix on the guest and a temporary FreeBSD-shaped root for regression tests. */
function bootstrapInstall(string $root, string $stage, array $receipt, Closure $startConfig): void {
	$state = '/var/db/freesense-bootstrap/receipt.json';
	$config = '/cf/conf/freesense/config.xml';
	$rc = '/etc/rc.conf.d/freesense_config';
	$enable = "freesense_config_enable=\"YES\"\n";
	foreach (['/usr/local/sbin/freesense-config', '/usr/local/etc/rc.d/freesense_config'] as $required) {
		bootRequire(isset($receipt[$required]), "Incomplete staged System payload: $required");
	}
	foreach ($receipt as $path => $attributes) {
		bootRequire((bool)preg_match('#^/usr/local/(?:[a-zA-Z0-9_.-]+/)*[a-zA-Z0-9_.-]+$#D', $path)
			&& !array_intersect(['.', '..'], explode('/', $path)), "Unreviewed install path: $path");
		bootRequire(in_array($attributes['mode'], [0444, 0644, 0555, 0755, 0600], true)
			&& bootMatches($stage . $path, $attributes), "Staged payload mismatch: $path");
	}
	$receipt[$rc] = ['sha256' => hash('sha256', $enable), 'mode' => 0644];
	$receiptText = json_encode($receipt, JSON_THROW_ON_ERROR) . "\n";
	foreach (['/etc/platform', '/etc/version', '/conf/config.xml', '/cf/conf/config.xml',
		'/usr/local/opnsense', '/etc/inc/globals.inc'] as $marker) {
		bootRequire(!file_exists($root . $marker) && !is_link($root . $marker), "Existing appliance marker: $marker");
	}
	foreach ([$state, $config, ...array_keys($receipt)] as $path) {
		bootParents($root, $path);
		bootRequire(!is_link($root . $path), "Refusing symlink: $path");
	}
	$owned = is_file($root . $state);
	if ($owned) {
		bootRequire(bootMatches($root . $state, ['sha256' => hash('sha256', $receiptText), 'mode' => 0600]),
			'Existing bootstrap receipt is modified or belongs to another payload.');
	} else {
		foreach ([dirname($state), dirname($config), '/usr/local/FreeSense'] as $unowned) {
			bootRequire(!file_exists($root . $unowned) && !is_link($root . $unowned), "Existing foreign tree: $unowned");
		}
	}
	foreach ($receipt as $path => $attributes) {
		if (file_exists($root . $path)) {
			bootRequire($owned && bootMatches($root . $path, $attributes), "Unowned or changed install file: $path");
		}
	}
	$lockPath = dirname($root . $config) . '/config.lock';
	foreach ([$root . $config, $lockPath] as $path) {
		bootRequire(!is_link($path), "Refusing linked configuration: $path");
		if (file_exists($path)) {
			bootRequire(is_file($path) && fileowner($path) === 0 && (fileperms($path) & 0077) === 0
				&& stat($path)['nlink'] === 1, "Unsafe configuration file: $path");
		}
	}
	$previous = is_file($root . $config) ? hash_file('sha256', $root . $config) : null;
	if (!$owned) {
		// Publish the ownership directory and its complete receipt together. Interrupted
		// staging must never leave an empty final directory that looks like a foreign install.
		$final = dirname($root . $state);
		bootCreateDir(dirname($final), 0755);
		$pending = $final . '.pending-' . bin2hex(random_bytes(12));
		bootRequire(mkdir($pending, 0700), 'Cannot stage ownership receipt.');
		try {
			bootCreateFile($pending . '/receipt.json', $receiptText, 0600);
			bootRequire(!file_exists($final) && rename($pending, $final), 'Cannot commit ownership receipt.');
		} finally {
			if (is_dir($pending)) {
				if (file_exists($pending . '/receipt.json')) {
					unlink($pending . '/receipt.json');
				}
				rmdir($pending);
			}
		}
	}
	foreach ($receipt as $path => $attributes) {
		if (!file_exists($root . $path)) {
			$bytes = $path === $rc ? $enable : file_get_contents($stage . $path);
			bootCreateFile($root . $path, $bytes, $attributes['mode']);
		}
		bootRequire(bootMatches($root . $path, $attributes), "Installed content mismatch: $path");
	}
	$startConfig();
	bootRequire(is_file($root . $config), 'Configuration service did not initialize a configuration.');
	if ($previous !== null) {
		bootRequire(hash_file('sha256', $root . $config) === $previous, 'Existing configuration changed during bootstrap.');
	}
	printf("Verified %d System files, enabled configuration service, %s configuration.\n",
		count($receipt) - 1, $previous === null ? 'initialized' : 'preserved');
}

if (realpath($_SERVER['SCRIPT_FILENAME']) === __FILE__) {
	bootRequire(PHP_OS === 'FreeBSD' && PHP_MAJOR_VERSION === 8 && PHP_MINOR_VERSION === 6,
		'Requires FreeBSD PHP 8.6.');
	// /var/db is root-owned. Acquire the same advisory lock on every attempt, including the first.
	bootParents('', '/var/db/.freesense-bootstrap.lock');
	$path = '/var/db/.freesense-bootstrap.lock';
	bootRequire(!is_link($path) && (!file_exists($path) || (is_file($path) && fileowner($path) === 0
		&& (fileperms($path) & 0077) === 0 && stat($path)['nlink'] === 1)), 'Unexpected bootstrap lock file.');
	$lock = fopen($path, 'c');
	bootRequire($lock !== false && flock($lock, LOCK_EX), 'Cannot acquire bootstrap lock.');
	$receipt = json_decode(file_get_contents(__DIR__ . '/receipt.json'), true, flags: JSON_THROW_ON_ERROR);
	bootstrapInstall('', __DIR__ . '/stage', $receipt, static function (): void {
		$enabled = [];
		exec('/usr/sbin/service -e', $enabled, $status);
		bootRequire($status === 0 && in_array('/usr/local/etc/rc.d/freesense_config', $enabled, true),
			'Configuration service is absent from the enabled boot order.');
		$process = proc_open(['/usr/sbin/service', 'freesense_config', 'start'], [], $pipes);
		bootRequire(is_resource($process) && proc_close($process) === 0, 'Configuration service start failed.');
	});
}
