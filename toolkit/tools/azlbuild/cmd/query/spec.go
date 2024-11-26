// Copyright Microsoft Corporation.
// Licensed under the MIT License.

package query

import (
	"log/slog"
	"path"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/microsoft/azurelinux/toolkit/tools/azlbuild/cmd"
	"github.com/microsoft/azurelinux/toolkit/tools/internal/rpm"
	"github.com/spf13/cobra"
)

var specPath string

var querySpecCmd = &cobra.Command{
	Use:   "spec",
	Short: "Query spec",
	RunE: func(c *cobra.Command, args []string) error {
		return querySpec(specPath, cmd.CmdEnv)
	},
	SilenceUsage: true,
}

type specInfo struct {
	Version      string   `json:"version"`
	PackageNames []string `json:"packages"`
}

func querySpec(specPath string, env *cmd.BuildEnv) error {
	slog.Info("Querying spec", "spec", specPath)

	absSpecPath, err := filepath.Abs(specPath)
	if err != nil {
		return err
	}

	buildArch, err := rpm.GetRpmArch(runtime.GOARCH)
	if err != nil {
		return err
	}

	const runChecks = false
	distTag, err := env.GetDistTag()
	if err != nil {
		return err
	}

	defines := rpm.DefaultDistroDefines(runChecks, distTag)

	results, err := rpm.QuerySPEC(absSpecPath, path.Dir(absSpecPath), "Name=%{name}\nVersion=%{version}\n", buildArch, defines)
	if err != nil {
		return err
	}

	var info specInfo
	for _, line := range results {
		kv := strings.SplitN(line, "=", 2)
		if len(kv) >= 2 {
			key := kv[0]
			value := kv[1]

			if key == "Version" {
				info.Version = value
			} else if key == "Name" {
				info.PackageNames = append(info.PackageNames, value)
			}
		}
	}

	err = cmd.ReportResult(info)
	if err != nil {
		return err
	}

	return nil
}

func init() {
	queryCmd.AddCommand(querySpecCmd)

	querySpecCmd.Flags().StringVarP(&specPath, "spec", "s", "", "spec file path")
	querySpecCmd.MarkFlagRequired("spec")
	querySpecCmd.MarkFlagFilename("spec")
}