module dota2backend

go 1.23

toolchain go1.23.12

require (
	github.com/dotabuff/manta v1.5.1
	google.golang.org/protobuf v1.36.11
)

require (
	github.com/davecgh/go-spew v1.1.0 // indirect
	github.com/golang/protobuf v1.5.4 // indirect
	github.com/golang/snappy v0.0.3 // indirect
)

replace github.com/dotabuff/manta => ./manta_patched
