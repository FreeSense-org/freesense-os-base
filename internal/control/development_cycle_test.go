package control

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func cycleFixture() DevelopmentCycle {
	hash := strings.Repeat("a", 64)
	revision := strings.Repeat("a", 40)
	plan, _ := json.Marshal(map[string]any{"schema_version": "freesense.multiarch-plan/v1", "pair_fingerprint": hash, "freebsd_pin": hash, "resolved_inputs": map[string]string{"freesense": revision}, "targets": map[string]any{"amd64": map[string]any{"system": map[string]any{}}, "arm64": map[string]any{"system": map[string]any{}}}})
	return DevelopmentCycle{SchemaVersion: DevelopmentCycleSchema, Generation: 7, Pin: hash, PairFingerprint: hash, Plan: plan,
		Sources:       map[string]string{"freesense": revision},
		Architectures: map[string]ArchitectureCycleStatus{"amd64": {}, "arm64": {}}}
}

func TestDevelopmentCycleResumesFrozenInputs(t *testing.T) {
	backend := newMemoryStore()
	cycle := cycleFixture()
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil || !updated {
		t.Fatalf("create: %v %v", updated, err)
	}
	cycle.Architectures["amd64"] = ArchitectureCycleStatus{System: "complete", Packages: "complete", Artifacts: "complete", Published: true}
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil || !updated {
		t.Fatalf("progress: %v %v", updated, err)
	}
	newer := cycleFixture()
	newer.Generation++
	newer.Sources["freesense"] = strings.Repeat("b", 40)
	var nextPlan map[string]any
	_ = json.Unmarshal(newer.Plan, &nextPlan)
	nextPlan["resolved_inputs"] = newer.Sources
	newer.Plan, _ = json.Marshal(nextPlan)
	if _, _, err := CommitDevelopmentCycle(context.Background(), backend, newer); err == nil {
		t.Fatal("replaced incomplete cycle")
	}
	cycle.Architectures["arm64"] = ArchitectureCycleStatus{System: "complete", Packages: "complete", Artifacts: "complete", Published: true}
	if _, _, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil {
		t.Fatal(err)
	}
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, newer); err != nil || !updated {
		t.Fatalf("advance completed cycle: %v %v", updated, err)
	}
}

func TestDevelopmentCycleSupersedeAllowsNewGeneration(t *testing.T) {
	backend := newMemoryStore()
	cycle := cycleFixture()
	if _, _, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil {
		t.Fatal(err)
	}
	cycle.Superseded = true
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil || !updated {
		t.Fatalf("supersede: %v %v", updated, err)
	}
	newer := cycleFixture()
	newer.Generation++
	newer.Sources["freesense"] = strings.Repeat("b", 40)
	var nextPlan map[string]any
	_ = json.Unmarshal(newer.Plan, &nextPlan)
	nextPlan["resolved_inputs"] = newer.Sources
	newer.Plan, _ = json.Marshal(nextPlan)
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, newer); err != nil || !updated {
		t.Fatalf("replace superseded cycle: %v %v", updated, err)
	}
}

func TestDevelopmentCycleSupersedeAllowsSameGenerationReplan(t *testing.T) {
	// state reserve-generation is keyed by pair fingerprint. A control-plane
	// fix that does not move that fingerprint reserves the same generation
	// again, so an abandoned cycle must be replaceable at its own generation.
	backend := newMemoryStore()
	cycle := cycleFixture()
	if _, _, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil {
		t.Fatal(err)
	}
	cycle.Superseded = true
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err != nil || !updated {
		t.Fatalf("supersede: %v %v", updated, err)
	}
	replan := cycleFixture()
	replan.Sources["freesense"] = strings.Repeat("c", 40)
	var nextPlan map[string]any
	_ = json.Unmarshal(replan.Plan, &nextPlan)
	nextPlan["resolved_inputs"] = replan.Sources
	replan.Plan, _ = json.Marshal(nextPlan)
	if replan.Generation != cycle.Generation {
		t.Fatalf("fixture must replan at the reserved generation")
	}
	if _, updated, err := CommitDevelopmentCycle(context.Background(), backend, replan); err != nil || !updated {
		t.Fatalf("replan at the reserved generation: %v %v", updated, err)
	}
	stored, err := backend.Get(context.Background(), DevelopmentCycleKey)
	if err != nil {
		t.Fatal(err)
	}
	var written DevelopmentCycle
	if err := json.Unmarshal(stored.Data, &written); err != nil {
		t.Fatal(err)
	}
	if written.Superseded {
		t.Fatal("the replanned cycle must not inherit supersession")
	}
	if !mapsEqual(written.Sources, replan.Sources) {
		t.Fatal("the replanned cycle kept the abandoned sources")
	}
}

func TestDevelopmentCycleRejectsSameGenerationInputRewrite(t *testing.T) {
	backend := newMemoryStore()
	cycle := cycleFixture()
	_, _, _ = CommitDevelopmentCycle(context.Background(), backend, cycle)
	cycle.Pin = strings.Repeat("b", 64)
	if _, _, err := CommitDevelopmentCycle(context.Background(), backend, cycle); err == nil {
		t.Fatal("rewrote frozen pin")
	}
}
