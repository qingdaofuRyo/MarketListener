# Agent任务交付

## 实现交付

**任务编号**：Android-16KB-compatibility

**结果**：已移除已弃用且不支持 16 KB 页大小的 `android-database-sqlcipher:4.5.4`，迁移至官方替代工件 `sqlcipher-android:4.6.1`。该版本是官方声明已支持 16 KB 页大小的最早版本，并与当前 Room 2.6.1 / Kotlin 2.0 构建组合兼容。

**修改文件**：`android/app/build.gradle.kts`、`android/app/src/main/java/com/marketmonitor/app/data/DatabaseBoundary.kt`。

**接口变化**：Room 的加密数据库工厂由 `net.sqlcipher.database.SupportFactory` 迁为 `net.zetetic.database.sqlcipher.SupportOpenHelperFactory`；创建个人数据库前显式执行 `System.loadLibrary("sqlcipher")`，这是新 SQLCipher 工件的必要初始化步骤。应用数据库名称、Keystore 密钥包装方式和市场/个人数据边界均未改变。

**实际测试**：在 `C:\Users\<user>\Documents\MarketListener`，使用 JDK 20.0.2 执行：

```powershell
android\gradlew.bat -p android testDebugUnitTest assembleDebug --no-daemon
```

结果：`BUILD SUCCESSFUL`，Android JVM 单元测试通过，生成 Debug APK。

对生成的 `android/app/build/outputs/apk/debug/app-debug.apk` 执行 Android SDK `zipalign -c -P 16 -v 4`：通过。读取 APK 内 SQLCipher ELF 程序头的实际结果：

- `lib/arm64-v8a/libsqlcipher.so`：所有 `PT_LOAD` 对齐值为 `0x4000`
- `lib/armeabi-v7a/libsqlcipher.so`：所有 `PT_LOAD` 对齐值为 `0x4000`
- `lib/x86/libsqlcipher.so`：所有 `PT_LOAD` 对齐值为 `0x4000`
- `lib/x86_64/libsqlcipher.so`：所有 `PT_LOAD` 对齐值为 `0x4000`

**风险**：`sqlcipher-android:4.17.0` 与当前 Room 2.6.1 不兼容，会触发 Kotlin 2.1 metadata 解析失败；本次选用官方已支持 16 KB 的 4.6.1 以保持既有构建组合稳定。后续如升级 SQLCipher，需要将 Room、Kotlin 和 AGP 作为一个经过完整回归的组合升级。

**未完成项**：尚未在 Pixel 10 Pro XL API 37.1 上重新安装此新 APK 并观察兼容性提示；需要由 Android Studio 重新部署后完成该设备侧验收。

**自检结果**：

- [x] 不再引用 `android-database-sqlcipher:4.5.4`
- [x] Debug APK 构建成功
- [x] Android JVM 单元测试通过
- [x] APK 16 KB ZIP 对齐通过
- [x] `x86_64/libsqlcipher.so` 的所有 `PT_LOAD` 段为 16 KB 对齐
