package io.legado.app.probe;

import android.content.res.AssetManager;

import org.robolectric.annotation.Implementation;
import org.robolectric.annotation.Implements;
import org.robolectric.shadows.ShadowArscAssetManager14;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.util.ArrayList;

import android.content.res.AssetFileDescriptor;
import android.os.ParcelFileDescriptor;

/**
 * Windows 专用垫片：把 assets 路径里的 {@code \} 归一成 {@code /}。
 *
 * <p><b>为什么需要它</b>：阅读的 {@code DefaultData} / {@code DirectLinkUpload} 用
 * {@code File.separator} 拼 assets 路径（{@code "defaultData${File.separator}xxx.json"}）。
 * Android 上 {@code File.separator} 恒为 {@code /}，但 JVM 跑在 Windows 时它是 {@code \}，
 * 而 {@code AssetManager} 只认 {@code /} → {@code FileNotFoundException}。
 *
 * <p><b>它是测试侧文件，不改 App 源码</b>——这正是「JVM 校验服务不入侵阅读源码」
 * 的那一环：上游一行不动，补丁只活在我们的 test source set 里。
 * Robolectric 在 {@code isIncludeAndroidResources = true}（本仓库已开）时按 SDK 选
 * {@code ShadowArscAssetManager14}，这里继承它，只把路径归一，其余行为原样透传。
 * 该链路上真正拦截 open 的是静态 native 方法，所以用 Java 写（静态方法可隐藏）
 * 而不是 Kotlin。
 *
 * <p>在 Linux/macOS 上跑时本 shadow 无害（没有 {@code \} 可归一）。
 */
@Implements(value = AssetManager.class)
public class WindowsPathAssetManagerShadow extends ShadowArscAssetManager14 {

    private static String normalize(String fileName) {
        return fileName == null ? null : fileName.replace('\\', '/');
    }

    @Implementation
    protected static long nativeOpenAsset(long asset, String fileName, int mode)
            throws FileNotFoundException {
        return ShadowArscAssetManager14.nativeOpenAsset(asset, normalize(fileName), mode);
    }

    @Implementation
    protected static ParcelFileDescriptor nativeOpenAssetFd(long asset, String fileName,
                                                           long[] outOffset)
            throws IOException {
        return ShadowArscAssetManager14.nativeOpenAssetFd(asset, normalize(fileName), outOffset);
    }

    @Implementation
    protected static long nativeOpenNonAsset(long asset, int cookie, String fileName, int mode)
            throws FileNotFoundException {
        return ShadowArscAssetManager14.nativeOpenNonAsset(asset, cookie, normalize(fileName), mode);
    }

    @Implementation
    protected static ParcelFileDescriptor nativeOpenNonAssetFd(long asset, int cookie,
                                                              String fileName, long[] outOffset)
            throws IOException {
        return ShadowArscAssetManager14.nativeOpenNonAssetFd(asset, cookie, normalize(fileName),
                outOffset);
    }

    @Implementation
    protected static long nativeOpenXmlAsset(long asset, int cookie, String fileName)
            throws FileNotFoundException {
        return ShadowArscAssetManager14.nativeOpenXmlAsset(asset, cookie, normalize(fileName));
    }
}
