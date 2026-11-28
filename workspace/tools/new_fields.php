<?php
declare(strict_types=1);
/* Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0 */
/* Dumps the schema of every form descriptor in lib/Http as JSON. Usage: php new_fields.php FREESENSE_SRC */
require $argv[1] . '/usr/local/FreeSense/autoload.php';
$out = [];
$describe = static function (string $name, string $case, string $value, object $form) use (&$out): void {
	$row = ['class' => $name, 'case' => $case, 'value' => $value];
	try { $row['path'] = $form->path(); $row['permission'] = $form->permission()->name; } catch (Throwable) {}
	try { $row['schema'] = $form->schema(); } catch (Throwable $e) { $row['error'] = $e::class; }
	$out[] = $row;
};
$library = $argv[1] . '/usr/local/FreeSense/lib/';
foreach (array_merge(glob($library . 'Http/*.php') ?: [], glob($library . 'Http/*/*.php') ?: []) as $file) {
	$name = basename($file, '.php');
	$class = 'FreeSense\\' . strtr(substr($file, strlen($library), -4), '/', '\\');
	try {
		if (!method_exists($class, 'schema')) continue;
		if (enum_exists($class)) {
			foreach ($class::cases() as $case) $describe($name, $case->name, (string) $case->value, $case);
		} else {
			// A form class holds no state that its schema reads, so skip the constructor and its collaborators.
			$describe($name, 'Form', '', (new ReflectionClass($class))->newInstanceWithoutConstructor());
		}
	} catch (Throwable $e) {
		$out[] = ['class' => $name, 'error' => $e::class];
	}
}
echo json_encode($out, JSON_UNESCAPED_SLASHES | JSON_PARTIAL_OUTPUT_ON_ERROR), "\n";
