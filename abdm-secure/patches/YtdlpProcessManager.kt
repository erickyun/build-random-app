package ir.amirab.downloader.downloaditem.ytdlp

import ir.amirab.util.platform.Platform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.BufferedInputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.zip.ZipInputStream

class FfmpegNotFoundException : Exception(
    "ffmpeg was not found. Install it and make sure it is on PATH, then try again."
)

object YtdlpProcessManager {
    private const val USER_AGENT = "ABDownloadManager-Secure/2.0.3"

    private const val YTDLP_VERSION = "2026.06.09"
    private const val YTDLP_WINDOWS_SHA256 =
        "3a48cb955d55c8821b60ccbdbbc6f61bc958f2f3d3b7ad5eaf3d83a543293a27"
    private const val YTDLP_WINDOWS_URL =
        "https://github.com/yt-dlp/yt-dlp/releases/download/$YTDLP_VERSION/yt-dlp.exe"

    private const val FFMPEG_RELEASE = "autobuild-2026-07-23-14-16"
    private const val FFMPEG_ARCHIVE_NAME = "ffmpeg-master-latest-win64-gpl.zip"
    private const val FFMPEG_CHECKSUMS_SHA256 =
        "a337d438b703df97be92dbb5642fe7b551b20834867c1eb399ddd09a8e0e38bc"
    private const val FFMPEG_BASE_URL =
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/$FFMPEG_RELEASE"

    private const val DENO_VERSION = "v2.9.4"
    private const val DENO_ARCHIVE_NAME = "deno-x86_64-pc-windows-msvc.zip"
    private const val DENO_BASE_URL =
        "https://github.com/denoland/deno/releases/download/$DENO_VERSION"

    private val installMutex = Mutex()
    private var exePathProvider: (() -> String)? = null

    fun init(provider: () -> String) {
        exePathProvider = provider
    }

    fun getExePath(): String = exePathProvider?.invoke() ?: "yt-dlp"

    private fun isWindows(): Boolean =
        Platform.getCurrentPlatform() == Platform.Desktop.Windows

    private fun ffmpegExeName(): String = if (isWindows()) "ffmpeg.exe" else "ffmpeg"

    private fun denoExeName(): String = if (isWindows()) "deno.exe" else "deno"

    private fun isExecutableOnPath(executable: String): Boolean {
        return try {
            val process = ProcessBuilder(executable, "--version").start()
            val exitCode = process.waitFor()
            process.destroy()
            exitCode == 0
        } catch (_: Exception) {
            false
        }
    }

    private fun isFfmpegAvailable(ytDlpDir: File): Boolean {
        if (File(ytDlpDir, ffmpegExeName()).isFile) return true
        return isExecutableOnPath(ffmpegExeName())
    }

    fun resolveFfmpegLocationArg(): File? {
        val ytDlpDir = File(getExePath()).parentFile ?: File(".")
        if (File(ytDlpDir, ffmpegExeName()).isFile) return ytDlpDir
        if (isExecutableOnPath(ffmpegExeName())) return null
        throw FfmpegNotFoundException()
    }

    fun resolveJsRuntimeArgs(): List<String> {
        val ytDlpDir = File(getExePath()).parentFile ?: File(".")
        val bundledDeno = File(ytDlpDir, denoExeName())
        return when {
            bundledDeno.isFile -> listOf("--js-runtimes", "deno:${bundledDeno.absolutePath}")
            isExecutableOnPath(denoExeName()) -> listOf("--js-runtimes", "deno")
            else -> emptyList()
        }
    }

    suspend fun ensureExecutableInstalled(onProgress: (Double) -> Unit = {}): File {
        return installMutex.withLock {
            withContext(Dispatchers.IO) {
                val executable = File(getExePath())
                val targetDir = executable.parentFile ?: File(".")
                targetDir.mkdirs()

                if (!isWindows()) {
                    if (!executable.isFile) {
                        throw IllegalStateException(
                            "Secure automatic yt-dlp installation currently supports Windows only. " +
                                "Install yt-dlp, ffmpeg and a supported JavaScript runtime manually."
                        )
                    }
                    return@withContext executable
                }

                ensureYtdlp(executable) { onProgress(it * 0.20) }
                ensureFfmpeg(targetDir) { onProgress(0.20 + it * 0.55) }
                ensureDeno(targetDir) { onProgress(0.75 + it * 0.25) }
                onProgress(1.0)
                executable
            }
        }
    }

    private fun ensureYtdlp(executable: File, onProgress: (Double) -> Unit) {
        if (executable.isFile && sha256(executable).equals(YTDLP_WINDOWS_SHA256, ignoreCase = true)) {
            onProgress(1.0)
            return
        }
        downloadVerified(YTDLP_WINDOWS_URL, executable, YTDLP_WINDOWS_SHA256, onProgress)
        executable.setExecutable(true)
    }

    private fun ensureFfmpeg(targetDir: File, onProgress: (Double) -> Unit) {
        val ffmpeg = File(targetDir, "ffmpeg.exe")
        val ffprobe = File(targetDir, "ffprobe.exe")
        if (ffmpeg.isFile && ffprobe.isFile) {
            onProgress(1.0)
            return
        }

        val checksums = File(targetDir, "ffmpeg-checksums.sha256.part")
        val archive = File(targetDir, "$FFMPEG_ARCHIVE_NAME.part")
        try {
            downloadVerified(
                "$FFMPEG_BASE_URL/checksums.sha256",
                checksums,
                FFMPEG_CHECKSUMS_SHA256
            ) { onProgress(it * 0.08) }

            val expectedArchiveHash = parseChecksumFile(checksums, FFMPEG_ARCHIVE_NAME)
            downloadVerified(
                "$FFMPEG_BASE_URL/$FFMPEG_ARCHIVE_NAME",
                archive,
                expectedArchiveHash
            ) { onProgress(0.08 + it * 0.82) }

            extractExecutables(
                archive,
                targetDir,
                mapOf("ffmpeg.exe" to ffmpeg, "ffprobe.exe" to ffprobe)
            )
            require(ffmpeg.isFile && ffprobe.isFile) { "FFmpeg archive did not contain expected files" }
            ffmpeg.setExecutable(true)
            ffprobe.setExecutable(true)
            onProgress(1.0)
        } finally {
            checksums.delete()
            archive.delete()
        }
    }

    private fun ensureDeno(targetDir: File, onProgress: (Double) -> Unit) {
        val deno = File(targetDir, "deno.exe")
        if (deno.isFile) {
            onProgress(1.0)
            return
        }

        val checksum = File(targetDir, "$DENO_ARCHIVE_NAME.sha256sum.part")
        val archive = File(targetDir, "$DENO_ARCHIVE_NAME.part")
        try {
            downloadUnverified(
                "$DENO_BASE_URL/$DENO_ARCHIVE_NAME.sha256sum",
                checksum
            ) { onProgress(it * 0.05) }
            val expectedArchiveHash = parseFirstChecksum(checksum)
            downloadVerified(
                "$DENO_BASE_URL/$DENO_ARCHIVE_NAME",
                archive,
                expectedArchiveHash
            ) { onProgress(0.05 + it * 0.85) }

            extractExecutables(archive, targetDir, mapOf("deno.exe" to deno))
            require(deno.isFile) { "Deno archive did not contain deno.exe" }
            deno.setExecutable(true)
            onProgress(1.0)
        } finally {
            checksum.delete()
            archive.delete()
        }
    }

    private fun downloadVerified(
        url: String,
        destination: File,
        expectedSha256: String,
        onProgress: (Double) -> Unit = {}
    ) {
        val temporary = File(destination.parentFile ?: File("."), destination.name + ".download")
        temporary.delete()
        try {
            downloadUnverified(url, temporary, onProgress)
            val actual = sha256(temporary)
            require(actual.equals(expectedSha256, ignoreCase = true)) {
                "SHA-256 verification failed for $url. Expected $expectedSha256 but received $actual"
            }
            replaceFile(temporary, destination)
        } finally {
            temporary.delete()
        }
    }

    private fun downloadUnverified(
        url: String,
        destination: File,
        onProgress: (Double) -> Unit = {}
    ) {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.instanceFollowRedirects = true
            connection.connectTimeout = 20_000
            connection.readTimeout = 60_000
            connection.setRequestProperty("User-Agent", USER_AGENT)
            connection.connect()
            require(connection.responseCode in 200..299) {
                "Download failed with HTTP ${connection.responseCode}: $url"
            }

            val totalSize = connection.contentLengthLong
            BufferedInputStream(connection.inputStream).use { input ->
                FileOutputStream(destination).use { output ->
                    val buffer = ByteArray(64 * 1024)
                    var totalRead = 0L
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        output.write(buffer, 0, count)
                        totalRead += count
                        if (totalSize > 0L) {
                            onProgress((totalRead.toDouble() / totalSize).coerceIn(0.0, 1.0))
                        }
                    }
                }
            }
            onProgress(1.0)
        } finally {
            connection.disconnect()
        }
    }

    private fun extractExecutables(
        archive: File,
        targetDir: File,
        targets: Map<String, File>
    ) {
        val extracted = mutableSetOf<String>()
        FileInputStream(archive).use { fileInput ->
            ZipInputStream(BufferedInputStream(fileInput)).use { zip ->
                while (true) {
                    val entry = zip.nextEntry ?: break
                    try {
                        if (entry.isDirectory) continue
                        val baseName = entry.name.replace('\\', '/').substringAfterLast('/')
                        val destination = targets[baseName.lowercase()] ?: continue
                        val part = File(targetDir, destination.name + ".extracting")
                        FileOutputStream(part).use { output -> zip.copyTo(output) }
                        replaceFile(part, destination)
                        extracted += baseName.lowercase()
                    } finally {
                        zip.closeEntry()
                    }
                }
            }
        }
        require(extracted.containsAll(targets.keys.map { it.lowercase() })) {
            "Archive was missing one or more required executables"
        }
    }

    private fun parseChecksumFile(file: File, requiredName: String): String {
        val match = file.useLines { lines ->
            lines.mapNotNull { parseChecksumLine(it) }
                .firstOrNull { (_, name) -> name == requiredName }
        }
        return requireNotNull(match?.first) { "Checksum for $requiredName was not found" }
    }

    private fun parseFirstChecksum(file: File): String {
        return file.useLines { lines ->
            lines.mapNotNull { parseChecksumLine(it) }.firstOrNull()?.first
        } ?: error("Checksum file was empty or invalid")
    }

    private fun parseChecksumLine(line: String): Pair<String, String>? {
        val trimmed = line.trim()
        if (trimmed.isEmpty()) return null
        val parts = trimmed.split(Regex("\\s+"), limit = 2)
        if (parts.size < 2 || !parts[0].matches(Regex("[0-9a-fA-F]{64}"))) return null
        return parts[0].lowercase() to parts[1].trim().removePrefix("*")
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
    }

    private fun replaceFile(source: File, destination: File) {
        destination.parentFile?.mkdirs()
        try {
            Files.move(
                source.toPath(),
                destination.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
                StandardCopyOption.ATOMIC_MOVE
            )
        } catch (_: Exception) {
            Files.move(
                source.toPath(),
                destination.toPath(),
                StandardCopyOption.REPLACE_EXISTING
            )
        }
    }
}
