package control

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"regexp"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

const DevelopmentCycleSchema = "freesense.development-cycle/v1"
const DevelopmentCycleKey = "state/development-cycle.json"

var sourceRevisionPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

type ArchitectureCycleStatus struct {
	System    string `json:"system"`
	Packages  string `json:"packages"`
	Artifacts string `json:"artifacts"`
	Published bool   `json:"published"`
}

type DevelopmentCycle struct {
	SchemaVersion   string                             `json:"schema_version"`
	Generation      uint64                             `json:"generation"`
	Pin             string                             `json:"pin"`
	PairFingerprint string                             `json:"pair_fingerprint"`
	Sources         map[string]string                  `json:"sources"`
	Plan            json.RawMessage                    `json:"plan"`
	Architectures   map[string]ArchitectureCycleStatus `json:"architectures"`
	Superseded      bool                               `json:"superseded,omitempty"`
}

func (cycle DevelopmentCycle) Validate() error {
	if cycle.SchemaVersion != DevelopmentCycleSchema || cycle.Generation == 0 || !fingerprintPattern.MatchString(cycle.Pin) || !fingerprintPattern.MatchString(cycle.PairFingerprint) {
		return errors.New("invalid Development cycle identity")
	}
	if len(cycle.Sources) == 0 || len(cycle.Plan) == 0 || len(cycle.Architectures) != 2 {
		return errors.New("Development cycle must freeze sources and both architecture plans")
	}
	var plan struct {
		SchemaVersion   string                     `json:"schema_version"`
		PairFingerprint string                     `json:"pair_fingerprint"`
		FreeBSDPin      string                     `json:"freebsd_pin"`
		Targets         map[string]json.RawMessage `json:"targets"`
		Resolved        map[string]string          `json:"resolved_inputs"`
	}
	if json.Unmarshal(cycle.Plan, &plan) != nil || plan.SchemaVersion != "freesense.multiarch-plan/v1" || plan.PairFingerprint != cycle.PairFingerprint || plan.FreeBSDPin != cycle.Pin || len(plan.Targets) != 2 || !mapsEqual(plan.Resolved, cycle.Sources) {
		return errors.New("Development cycle plan differs from its frozen identity")
	}
	for _, arch := range []string{"amd64", "arm64"} {
		if len(plan.Targets[arch]) == 0 {
			return fmt.Errorf("invalid %s cycle plan", arch)
		}
		status, ok := cycle.Architectures[arch]
		if !ok {
			return fmt.Errorf("missing %s cycle status", arch)
		}
		for _, value := range []string{status.System, status.Packages, status.Artifacts} {
			if value != "" && value != "pending" && value != "complete" {
				return fmt.Errorf("invalid %s cycle status", arch)
			}
		}
		if status.Published && (status.System != "complete" || status.Packages != "complete" || status.Artifacts != "complete") {
			return fmt.Errorf("%s cannot publish incomplete artifacts", arch)
		}
	}
	for name, revision := range cycle.Sources {
		if name == "" || !sourceRevisionPattern.MatchString(revision) {
			return errors.New("invalid frozen source revision")
		}
	}
	return nil
}

// CommitDevelopmentCycle provides the single-writer resume point. A pending
// cycle cannot be replaced by newer sources; only monotonic status updates for
// the same generation are accepted until completion or explicit supersession.
func CommitDevelopmentCycle(ctx context.Context, backend store.Backend, next DevelopmentCycle) (store.ObjectInfo, bool, error) {
	if err := next.Validate(); err != nil {
		return store.ObjectInfo{}, false, err
	}
	for attempt := 0; attempt < 5; attempt++ {
		raw, err := json.Marshal(next)
		if err != nil {
			return store.ObjectInfo{}, false, err
		}
		raw = append(raw, '\n')
		current, getErr := backend.Get(ctx, DevelopmentCycleKey)
		if errors.Is(getErr, store.ErrNotFound) {
			info, created, putErr := backend.PutIfAbsent(ctx, DevelopmentCycleKey, store.BytesContent(raw))
			if putErr == nil && created {
				return info, true, nil
			}
			if putErr != nil && !errors.Is(putErr, store.ErrPrecondition) {
				return store.ObjectInfo{}, false, putErr
			}
			continue
		}
		if getErr != nil {
			return store.ObjectInfo{}, false, getErr
		}
		var old DevelopmentCycle
		if json.Unmarshal(current.Data, &old) != nil || old.Validate() != nil {
			return store.ObjectInfo{}, false, errors.New("invalid existing Development cycle")
		}
		if next.Generation < old.Generation {
			return store.ObjectInfo{}, false, errors.New("Development cycle cannot move backwards")
		}
		if next.Generation == old.Generation {
			if next.Pin != old.Pin || next.PairFingerprint != old.PairFingerprint || !mapsEqual(next.Sources, old.Sources) || !rawJSONEqual(next.Plan, old.Plan) {
				return store.ObjectInfo{}, false, errors.New("frozen Development cycle inputs cannot change")
			}
			for _, arch := range []string{"amd64", "arm64"} {
				before, after := old.Architectures[arch], next.Architectures[arch]
				if regressed(before.System, after.System) {
					after.System = before.System
				}
				if regressed(before.Packages, after.Packages) {
					after.Packages = before.Packages
				}
				if regressed(before.Artifacts, after.Artifacts) {
					after.Artifacts = before.Artifacts
				}
				if before.Published {
					after.Published = true
				}
				next.Architectures[arch] = after
			}
			raw, err = json.Marshal(next)
			if err != nil {
				return store.ObjectInfo{}, false, err
			}
			raw = append(raw, '\n')
			if bytes.Equal(raw, current.Data) {
				return store.ObjectInfo{Key: current.Key, Size: current.Size, ETag: current.ETag, SHA256: current.SHA256}, false, nil
			}
		} else if !old.Superseded && !(old.Architectures["amd64"].Published && old.Architectures["arm64"].Published) {
			return store.ObjectInfo{}, false, errors.New("incomplete Development cycle must be resumed")
		}
		info, swapErr := backend.CompareAndSwap(ctx, DevelopmentCycleKey, current.ETag, store.BytesContent(raw))
		if swapErr == nil {
			return info, true, nil
		}
		if !errors.Is(swapErr, store.ErrPrecondition) {
			return store.ObjectInfo{}, false, swapErr
		}
	}
	return store.ObjectInfo{}, false, errors.New("Development cycle changed repeatedly")
}

func mapsEqual(a, b map[string]string) bool {
	ar, _ := json.Marshal(a)
	br, _ := json.Marshal(b)
	return bytes.Equal(ar, br)
}
func rawJSONEqual(a, b json.RawMessage) bool {
	var av, bv any
	return json.Unmarshal(a, &av) == nil && json.Unmarshal(b, &bv) == nil && reflect.DeepEqual(av, bv)
}

func LoadDevelopmentCycle(ctx context.Context, backend store.Backend) (DevelopmentCycle, bool, error) {
	object, err := backend.Get(ctx, DevelopmentCycleKey)
	if errors.Is(err, store.ErrNotFound) {
		return DevelopmentCycle{}, false, nil
	}
	if err != nil {
		return DevelopmentCycle{}, false, err
	}
	var cycle DevelopmentCycle
	if json.Unmarshal(object.Data, &cycle) != nil || cycle.Validate() != nil {
		return DevelopmentCycle{}, false, errors.New("invalid existing Development cycle")
	}
	return cycle, true, nil
}
func regressed(before, after string) bool { return before == "complete" && after != "complete" }
