<?php
declare(strict_types=1);
/* Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0 */
/* The bootstrap invokes this regression in its private temporary directory, before guest admission. */
require __DIR__ . '/bootstrap-install.php';

function expectedRefusal(Closure $call, string $reason): void {
	try {
		$call();
	} catch (RuntimeException $error) {
		bootRequire(str_contains($error->getMessage(), $reason), 'Unexpected refusal: ' . $error->getMessage());
		return;
	}
	throw new RuntimeException('Missing refusal: ' . $reason);
}

$base = getcwd() . '/bootstrap-regression';
bootRequire(!file_exists($base) && mkdir($base, 0700), 'Expected a private clean work directory');
$stage = $base . '/stage';
$cli = '/usr/local/sbin/freesense-config';
$rc = '/usr/local/etc/rc.d/freesense_config';
$receipt = [];
foreach ([$cli, $rc] as $path) {
	bootCreateFile($stage . $path, "synthetic fixture\n", 0555);
	$receipt[$path] = ['sha256' => hash_file('sha256', $stage . $path), 'mode' => 0555];
}
$root = $base . '/root';
bootCreateDir($root, 0700);
$conf = $root . '/cf/conf/freesense/config.xml';
$run = static function () use ($root, $stage, $receipt, $conf): void {
	bootstrapInstall($root, $stage, $receipt, static function () use ($conf): void {
		if (!is_file($conf)) {
			bootCreateFile($conf, '<synthetic/>', 0600);
		}
	});
};
bootCreateFile($root . $rc, 'foreign', 0444);
expectedRefusal($run, 'Unowned or changed install file');
bootRequire(!file_exists($root . $cli), 'Collision must precede the first file');
unlink($root . $rc);
bootCreateFile($root . '/etc/platform', 'synthetic', 0444);
expectedRefusal($run, 'Existing appliance marker');
unlink($root . '/etc/platform');
bootCreateDir($root . '/usr/local/sbin-tmp', 0755);
symlink($root . '/usr/local/sbin-tmp', $root . '/usr/local/sbin');
expectedRefusal($run, 'Symlink in install path');
unlink($root . '/usr/local/sbin');
$run();
file_put_contents($conf, '<custom/>');
$run();
bootRequire(file_get_contents($conf) === '<custom/>', 'Preserved configuration');
unlink($root . $cli);
$run();
bootRequire(bootMatches($root . $cli, $receipt[$cli]), 'Resume missing authorized file');
chmod($root . $cli, 0777);
expectedRefusal($run, 'Unowned or changed install file');
echo "Bootstrap collisions, symlinks, clean install, repeat, recovery and drift checks passed.\n";
