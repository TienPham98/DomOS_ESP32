module github.com/domos/backend-go

go 1.25.0

replace (
	golang.org/x/crypto => github.com/golang/crypto v0.23.0
	golang.org/x/net => github.com/golang/net v0.23.0
	golang.org/x/sync => github.com/golang/sync v0.7.0
	golang.org/x/sys => github.com/golang/sys v0.47.0
	golang.org/x/text => github.com/golang/text v0.15.0
	gopkg.in/check.v1 => github.com/go-check/check v0.0.0-20161208181325-20d25e280405
	gopkg.in/yaml.v3 => github.com/go-yaml/yaml v3.0.1+incompatible
	gorm.io/driver/postgres => github.com/go-gorm/postgres v1.5.9
	gorm.io/gorm => github.com/go-gorm/gorm v1.25.10
)

require (
	github.com/eclipse/paho.mqtt.golang v1.4.3
	github.com/glebarez/sqlite v1.11.0
	github.com/gofiber/fiber/v2 v2.52.5
	github.com/gofiber/websocket/v2 v2.2.1
	github.com/golang-jwt/jwt/v5 v5.2.1
	github.com/google/uuid v1.6.0
	github.com/joho/godotenv v1.5.1
	golang.org/x/crypto v0.23.0
	gorm.io/driver/postgres v1.5.9
	gorm.io/gorm v1.25.10
)

require (
	github.com/andybalholm/brotli v1.0.5 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/fasthttp/websocket v1.5.3 // indirect
	github.com/glebarez/go-sqlite v1.21.2 // indirect
	github.com/gorilla/websocket v1.5.0 // indirect
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20221227161230-091c0ba34f0a // indirect
	github.com/jackc/pgx/v5 v5.5.5 // indirect
	github.com/jackc/puddle/v2 v2.2.1 // indirect
	github.com/jinzhu/inflection v1.0.0 // indirect
	github.com/jinzhu/now v1.1.5 // indirect
	github.com/klauspost/compress v1.17.0 // indirect
	github.com/mattn/go-colorable v0.1.13 // indirect
	github.com/mattn/go-isatty v0.0.24 // indirect
	github.com/mattn/go-runewidth v0.0.15 // indirect
	github.com/ncruces/go-strftime v1.0.0 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	github.com/rivo/uniseg v0.2.0 // indirect
	github.com/savsgio/gotils v0.0.0-20230208104028-c358bd845dee // indirect
	github.com/valyala/bytebufferpool v1.0.0 // indirect
	github.com/valyala/fasthttp v1.51.0 // indirect
	github.com/valyala/tcplisten v1.0.0 // indirect
	golang.org/x/net v0.21.0 // indirect
	golang.org/x/sync v0.21.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.15.0 // indirect
	modernc.org/libc v1.74.4 // indirect
	modernc.org/mathutil v1.7.1 // indirect
	modernc.org/memory v1.11.0 // indirect
	modernc.org/sqlite v1.56.0 // indirect
)
